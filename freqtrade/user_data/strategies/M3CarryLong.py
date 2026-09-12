"""M3-DSH 实验 C: 负费率 Carry 多头 (吃资金费 + 趋势延续).

假设(基于已实测的数据, 不是猜):
    1. 资金费率与未来收益的关系: 实测「年化费率 < -7%」这一桶在 15/60/240 分钟三个
       时间尺度上都是表现最好的桶之一(-7.7% 以下桶未来 60 分钟 +0.072%)。
    2. 持有负费率多头时, 空头要**付钱给**我们 —— 持仓期间持续收钱。
       实测当前持仓时长中位仅 18 分钟, 几乎收不到资金费;
       因此本实验刻意把持有期拉长到小时级, 让 Carry 真正成为收益来源。
    3. 涨幅榜标的往往费率极端为负(空头拥挤/挤空行情), 与「24h 大幅上涨」叠加时
       容易出现「涨 + 收钱」的双击。

与 A/B 的区别:
    A = 追动量延续(短持, 高频)          B = 急跌反弹(短持)
    C = 负费率 + 趋势延续 + **长持**(小时级), 让资金费成为收益项

风控与 A/B 共用 stops_core(风险预算/距离钳制/四不变量), 唯一差别是择时与持有期。
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

log = logging.getLogger("freqtrade.M3CarryLong")
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"


def _utc(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


class M3CarryLong(IStrategy):
    """负费率 Carry 多头: 收着资金费跟随趋势."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    ignore_roi_if_entry_signal = True
    startup_candle_count = 200

    use_custom_stoploss = True
    position_adjustment_enable = True
    stoploss = -0.20
    minimal_roi = {}

    # ---- 核心门槛: 负费率(收钱) + 涨幅榜在榜 + 趋势未破 ----
    MAX_FUNDING_ANN = -0.30        # 年化费率必须 <= -30%(持有期间收费率)
    MIN_CHANGE_24H = 5.0           # 24h 涨幅为正(趋势票)
    MIN_STICK = 0.30               # 榜单稳定性(避免脉冲票)
    MIN_QUOTE_VOL = 20_000_000.0
    MAX_SPREAD_BPS = 20.0
    MIN_RSI4 = 25.0                # 4h 不能超卖(排除崩盘)
    MAX_RSI4 = 85.0

    # ---- 长持设计: 给 Carry 足够时间累积 ----
    MAX_HOLD_HOURS = 48
    TRAIL_START_PROFIT = 0.03
    TRAIL_ATR_MULT = 3.0           # 长持 -> 跟踪距离放宽, 不要被日常波动打断
    PROFIT_PROTECT = 0.5
    PARTIAL_TIERS = (0.10,)        # 只在高利润处减一次, 其余继续收 Carry
    PARTIAL_RATIO = 0.4

    # ---- 风控(与 A/B 同口径, 长持 -> 单笔风险略低) ----
    leverage_value = 2.0           # 长持 + 费率不确定 -> 降低杠杆
    max_leverage = 2.0
    STOP_ATR_MULT = 3.0            # 长持需要更宽的止损
    HARD_STOP = -0.09
    RISK_BUDGET = 0.008
    RISK_CEILING = 0.012
    MIN_STAKE_RATIO = 0.04
    MAX_STAKE_RATIO = 0.30
    TARGET_STAKE_RATIO = 0.18
    REENTRY_COOLDOWN_MIN = 60

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._live_funding: dict[str, dict[str, float]] = {}
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig: dict[str, int] = {}
        log.info("[CARRY] 负费率 Carry 多头启动 (%s)", _utc(int(time.time() * 1000)))
        log.info("[CARRY] 门槛: 年化费率<=%.0f%% 24h涨幅>=%.1f%% 稳定性>=%.2f 杠杆%.1fx "
                 "最长持有%dh 跟踪=%.1f×ATR 风险预算%.1f%%/上限%.1f%%",
                 self.MAX_FUNDING_ANN * 100, self.MIN_CHANGE_24H, self.MIN_STICK,
                 self.leverage_value, self.MAX_HOLD_HOURS, self.TRAIL_ATR_MULT,
                 self.RISK_BUDGET * 100, self.RISK_CEILING * 100)

    # ------------------------------------------------------------ 外部数据
    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["up"] = ((dataframe["close"] > dataframe["ema50"]) &
                           (dataframe["ema21"] > dataframe["ema50"] * 0.99)).astype(float)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["mom_1h"] = dataframe["close"].pct_change(12) * 100.0
        self._attach(dataframe, metadata)
        return dataframe

    def _attach(self, dataframe: DataFrame, metadata: dict) -> None:
        pair = metadata.get("pair", "")
        sym = pair.split("/")[0] + "USDT" if pair else ""
        self._refresh()
        wl = self._wl.get(sym, {})
        dataframe["m3_funding_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)
        dataframe["m3_stick"] = float(wl.get("stability", 0.0) or 0.0)
        dataframe["m3_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_spread"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_in_pool"] = bool(wl)
        dataframe["m3_oi"] = float(wl.get("oi_chg_1h", 0.0) or 0.0)

    def _refresh(self) -> None:
        now = int(time.time() * 1000)
        if now - self._wl_ms < 20_000 and self._wl:
            return
        try:
            raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            self._wl = {c["symbol"]: c for c in raw.get("candidates", []) if c.get("symbol")}
            self._wl_ms = now
        except Exception as exc:  # noqa: BLE001
            if now - self._wl_ms > 300_000:
                log.warning("[CARRY] watchlist 读取失败: %s", exc)
                self._wl_ms = now

    # ------------------------------------------------------------ 入场
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cols = dataframe.columns
        up1h = dataframe.get("up_1h", pd.Series(1.0, index=dataframe.index)).fillna(1.0)
        rsi4 = dataframe.get("rsi_1h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)
        stick_ok = (dataframe["m3_stick"] >= self.MIN_STICK) | (dataframe["m3_stick"] == 0.0)

        ok = (dataframe["m3_in_pool"].astype(bool)
              & (dataframe["m3_funding_ann"] <= self.MAX_FUNDING_ANN)   # 核心: 持有收费率
              & (dataframe["m3_change_24h"] >= self.MIN_CHANGE_24H)     # 趋势向上
              & stick_ok
              & (dataframe["m3_vol"] >= self.MIN_QUOTE_VOL)
              & (dataframe["m3_spread"] <= self.MAX_SPREAD_BPS)
              & (up1h >= 0.5)                                           # 1h 趋势未破
              & (dataframe["close"] > dataframe["ema21"])               # 5m 站上均线
              & (dataframe["mom_1h"] > -3.0)                            # 不是正在暴跌
              & (rsi4 >= self.MIN_RSI4) & (rsi4 <= self.MAX_RSI4)
              & (dataframe["m3_oi"] > -10.0)
              & (dataframe["volume"] > 0))
        dataframe.loc[ok, ["enter_long", "enter_tag"]] = (1, "carry_long")

        i = len(dataframe) - 1
        st = self._sig
        st["n"] = st.get("n", 0) + 1
        st["sig"] = st.get("sig", 0) + int(bool(ok.iloc[i]))
        gates = {
            "pool": bool(dataframe["m3_in_pool"].iloc[i]),
            "neg_funding": float(dataframe["m3_funding_ann"].iloc[i] or 0) <= self.MAX_FUNDING_ANN,
            "chg24": float(dataframe["m3_change_24h"].iloc[i] or 0) >= self.MIN_CHANGE_24H,
            "stick": bool(stick_ok.iloc[i]),
            "up1h": float(up1h.iloc[i]) >= 0.5,
            "above_ema": bool(dataframe["close"].iloc[i] > dataframe["ema21"].iloc[i]),
            "liq": float(dataframe["m3_vol"].iloc[i] or 0) >= self.MIN_QUOTE_VOL,
        }
        acc = st.setdefault("g", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["n"] % 100 == 0:
            log.info("[CARRY] 评估 %d 次, 信号 %d 次 | 门槛通过率: %s", st["n"], st["sig"],
                     "  ".join("%s=%.0f%%" % (k, acc.get(k, 0) / st["n"] * 100) for k in gates))
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ------------------------------------------------------------ 仓位/杠杆
    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs: Any) -> float:
        atr = 0.03
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and len(df):
                atr = max(float(df.iloc[-1].get("atr_pct", 3.0) or 3.0) / 100.0, 0.005)
        except Exception:  # noqa: BLE001
            pass
        sd = entry_stop_distance(atr, self.STOP_ATR_MULT)
        safe = leverage_for_risk(sd, max_leverage=max_leverage,
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
            sd = entry_stop_distance(atr_pct / 100.0, self.STOP_ATR_MULT)
            wallet = self.wallets.get_total_stake_amount() if self.wallets else 0.0
            if wallet <= 0:
                return proposed_stake
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0)
            # 费率越负(收得越多)越值得下注; 这正是本实验的核心变量
            conf = 1.0
            if ann <= -1.0:
                conf = 1.35
            elif ann <= -0.5:
                conf = 1.20
            elif ann <= -0.3:
                conf = 1.10
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=sd, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=ann)
            if not plan.ok:
                log.warning("[CARRY] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            if min_stake and stake < min_stake:
                stake = min(min_stake, max_stake * 0.9 if max_stake else min_stake)
            self._diag[pair] = {"stop_dist": sd, "leverage": float(leverage),
                                "stake": float(stake), "design_risk": float(plan.risk_pct),
                                "ann": ann}
            log.info("[CARRY] %s 仓位: 费率年化%.0f%% 止损距离%.2f%% 杠杆%.1f -> 保证金%.2f "
                     "(名义%.2f, 设计风险%.2f%%) [%s]", pair, ann * 100, sd * 100, leverage,
                     stake, stake * leverage, plan.risk_pct * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[CARRY] 仓位异常 %s: %s", pair, exc)
            return proposed_stake

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs: Any) -> Optional[float]:
        lev = float(trade.leverage or 1.0)
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            atr = float(row.get("atr_pct", 3.0) or 3.0) / 100.0 if row is not None else 0.03
        except Exception:  # noqa: BLE001
            atr = 0.03
        peak = current_profit
        try:
            if trade.max_rate and not trade.is_short:
                peak = (trade.max_rate - trade.open_rate) / trade.open_rate * lev
        except Exception:  # noqa: BLE001
            pass
        p = StopParams(stop_atr_mult=self.STOP_ATR_MULT, trail_atr_mult=self.TRAIL_ATR_MULT,
                       trail_start_profit=self.TRAIL_START_PROFIT, hard_stop=self.HARD_STOP,
                       profit_protect=self.PROFIT_PROTECT)
        d, _ = stop_price_distance(current_profit, lev, atr, p, peak_profit=peak)
        sp = stop_rate(current_rate, d, False)
        val = freqtrade_stoploss_value(current_rate, sp, lev, False)
        return float(val) if val > 0 else None

    def adjust_trade_position(self, trade: Trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs: Any) -> Optional[float]:
        if trade.has_open_orders:
            return None
        n = int(trade.nr_of_successful_exits or 0)
        if n >= len(self.PARTIAL_TIERS) or current_profit < self.PARTIAL_TIERS[n]:
            return None
        amount = trade.stake_amount * self.PARTIAL_RATIO
        if min_stake and amount < min_stake:
            return None
        log.info("[CARRY] %s 止盈减仓: 浮盈%.2f%% 减%.2f", trade.pair, current_profit * 100, amount)
        return -float(amount)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        """离场条件: 费率不再为负(不再收钱) 或 超时."""
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0) if row is not None else 0.0
        except Exception:  # noqa: BLE001
            ann = 0.0
        # 费率转正 -> 持有不再收钱, 失去本策略的核心逻辑
        if ann > 0.05:
            return "carry_funding_flipped"
        if hold_h > self.MAX_HOLD_HOURS:
            return "carry_max_hold"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        if d:
            log.info("[CARRY][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 入场费率年化%.0f%% | "
                     "资金费%+.4f | 持有%.1f小时", pair, exit_reason, d["design_risk"] * 100,
                     ratio * 100, d["ann"] * 100, trade.funding_fees or 0.0, hold_h)
        return True
