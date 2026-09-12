"""M3-DSH 实验 D: 波动压缩突破 (吸筹后放量突破).

假设:
    涨幅榜标的在爆发前常有一段「波动收敛 + 缩量横盘」的蓄势期, 随后放量突破。
    与 A 的差别: A 在「已经大涨」之后追延续; D 在**突破发生的那一根**入场,
    目标是吃到爆发的第一段, 而不是等涨了 20% 才上车。

已实测支撑:
    - 「过去 15 分钟涨超 5%」的未来 60 分钟收益 +3.901%(胜率 56.5%),
      说明强势突破确有延续性 —— 但 A 需要标的已进入涨幅榜前列才会考虑,
      而 D 直接从「压缩 -> 突破」这个事件入手, 理论上更早。
    - 「24h 涨幅 10~20%」是负期望区(-0.312%), 因此 D **不**要求 24h 涨幅,
      只在突破当根 + 放量确认时入场, 避开中间地带。

风控与 A/B/C 共用 stops_core(四不变量), 保证可比。
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

from stops_core import (StopParams, entry_stop_distance, freqtrade_stoploss_value,  # noqa: E402
                        leverage_for_risk, plan_position, stop_price_distance, stop_rate)

log = logging.getLogger("freqtrade.M3VolBreakout")
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"


class M3VolBreakout(IStrategy):
    """波动压缩后放量突破: 买在爆发第一根."""

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

    # ---- 突破/压缩门槛 ----
    RANGE_WINDOW = 12          # 12×5m = 1 小时压缩窗口
    COMPRESSION_MAX = 0.6      # 当前 1 小时振幅 / 过去 4 小时平均振幅 <= 0.6 视为压缩
    BREAKOUT_LOOKBACK = 6      # 突破近 6 根(30 分钟)高点
    VOLUME_MULT = 2.0          # 突破当根成交量 >= 20 根均量的 2 倍
    MIN_QUOTE_VOL = 15_000_000.0
    MAX_SPREAD_BPS = 15.0
    MIN_RSI5 = 55.0            # 突破时应有动量
    MAX_RSI4 = 80.0

    # ---- 持有设计: 中等时长(数小时) ----
    MAX_HOLD_HOURS = 12
    TRAIL_START_PROFIT = 0.025
    TRAIL_ATR_MULT = 2.6
    PROFIT_PROTECT = 0.6
    PARTIAL_TIERS = (0.06, 0.15)
    PARTIAL_RATIO = 0.35

    # ---- 风控 ----
    leverage_value = 3.0
    max_leverage = 3.0
    STOP_ATR_MULT = 2.4
    HARD_STOP = -0.075
    RISK_BUDGET = 0.008
    RISK_CEILING = 0.012
    MIN_STAKE_RATIO = 0.04
    MAX_STAKE_RATIO = 0.30
    TARGET_STAKE_RATIO = 0.20
    REENTRY_COOLDOWN_MIN = 30

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig: dict[str, int] = {}
        log.info("[VOL] 波动压缩突破策略启动 (UTC %s)",
                 datetime.utcfromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S"))
        log.info("[VOL] 门槛: 压缩比<=%.2f 突破近%d根 量能>=%.1fx RSI5>=%.0f 杠杆%.1fx "
                 "风险预算%.1f%%/上限%.1f%%",
                 self.COMPRESSION_MAX, self.BREAKOUT_LOOKBACK, self.VOLUME_MULT,
                 self.MIN_RSI5, self.leverage_value, self.RISK_BUDGET * 100,
                 self.RISK_CEILING * 100)

    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        # 波动压缩: 当前 1 小时振幅 / 过去 4 小时平均小时振幅
        hi, lo = dataframe["high"], dataframe["low"]
        rng = (hi.rolling(self.RANGE_WINDOW).max() - lo.rolling(self.RANGE_WINDOW).min())
        rng_pct = rng / dataframe["close"]
        baseline = rng_pct.rolling(self.RANGE_WINDOW * 4).mean()
        dataframe["compression"] = rng_pct / baseline.replace(0, float("nan"))
        # 突破
        dataframe["hh"] = hi.rolling(self.BREAKOUT_LOOKBACK).max().shift(1)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, float("nan"))
        dataframe["breakout"] = (dataframe["close"] > dataframe["hh"]) & \
                                (dataframe["vol_ratio"] >= self.VOLUME_MULT)
        self._attach(dataframe, metadata)
        return dataframe

    def _attach(self, dataframe: DataFrame, metadata: dict) -> None:
        pair = metadata.get("pair", "")
        sym = pair.split("/")[0] + "USDT" if pair else ""
        self._refresh()
        wl = self._wl.get(sym, {})
        dataframe["m3_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_spread"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_funding_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)
        dataframe["m3_in_pool"] = bool(wl)

    def _refresh(self) -> None:
        now = int(time.time() * 1000)
        if now - self._wl_ms < 20_000 and self._wl:
            return
        try:
            raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            self._wl = {c["symbol"]: c for c in raw.get("candidates", []) if c.get("symbol")}
            self._wl_ms = now
        except Exception:  # noqa: BLE001
            self._wl_ms = now

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rsi4 = dataframe.get("rsi_1h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)
        ok = (dataframe["m3_in_pool"].astype(bool)
              & (dataframe["compression"] <= self.COMPRESSION_MAX)     # 蓄势
              & (dataframe["breakout"])                                # 放量突破
              & (dataframe["rsi"] >= self.MIN_RSI5)                    # 有动量
              & (rsi4 <= self.MAX_RSI4)                                # 4h 未极端超买
              & (dataframe["m3_vol"] >= self.MIN_QUOTE_VOL)
              & (dataframe["m3_spread"] <= self.MAX_SPREAD_BPS)
              & (dataframe["m3_funding_ann"] <= 0.50)                  # 拒绝高费率
              & (dataframe["m3_change_24h"] > -20.0)
              & (dataframe["volume"] > 0))
        dataframe.loc[ok, ["enter_long", "enter_tag"]] = (1, "vol_breakout")

        i = len(dataframe) - 1
        st = self._sig
        st["n"] = st.get("n", 0) + 1
        st["sig"] = st.get("sig", 0) + int(bool(ok.iloc[i]))
        gates = {
            "pool": bool(dataframe["m3_in_pool"].iloc[i]),
            "compression": bool((dataframe["compression"].iloc[i] or 9) <= self.COMPRESSION_MAX),
            "breakout": bool(dataframe["breakout"].iloc[i]),
            "rsi5": bool((dataframe["rsi"].iloc[i] or 0) >= self.MIN_RSI5),
            "liq": bool((dataframe["m3_vol"].iloc[i] or 0) >= self.MIN_QUOTE_VOL),
        }
        acc = st.setdefault("g", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["n"] % 100 == 0:
            log.info("[VOL] 评估 %d 次, 信号 %d 次 | 门槛通过率: %s", st["n"], st["sig"],
                     "  ".join("%s=%.0f%%" % (k, acc.get(k, 0) / st["n"] * 100) for k in gates))
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

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
            vr = float(row.get("vol_ratio", 0) or 0)
            conf = 1.0 + min(max(vr - 2.0, 0.0), 3.0) * 0.15     # 量能越强越有信心
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=sd, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=float(row.get("m3_funding_ann", 0) or 0))
            if not plan.ok:
                log.warning("[VOL] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            if min_stake and stake < min_stake:
                stake = min(min_stake, max_stake * 0.9 if max_stake else min_stake)
            self._diag[pair] = {"stop_dist": sd, "leverage": float(leverage), "stake": float(stake),
                                "design_risk": float(plan.risk_pct), "vol_ratio": vr}
            log.info("[VOL] %s 仓位: 量比%.1fx 止损距离%.2f%% 杠杆%.1f -> 保证金%.2f "
                     "(设计风险%.2f%%) [%s]", pair, vr, sd * 100, leverage, stake,
                     plan.risk_pct * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[VOL] 仓位异常 %s: %s", pair, exc)
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
        log.info("[VOL] %s 阶梯止盈第%d档: 浮盈%.2f%% 减%.2f",
                 trade.pair, n + 1, current_profit * 100, amount)
        return -float(amount)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        if hold_h > self.MAX_HOLD_HOURS:
            return "vol_max_hold"
        # 突破失败: 浮盈回落且跌破入场价 -> 及早离场(突破逻辑已失效)
        if current_profit < 0 and hold_h > 2.0:
            return "vol_breakout_failed"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[VOL][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 量比%.1fx "
                     "资金费%+.4f | 持有%.1f小时", pair, exit_reason, d["design_risk"] * 100,
                     ratio * 100, d.get("vol_ratio", 0), trade.funding_fees or 0.0,
                     (current_time - trade.open_date_utc).total_seconds() / 3600.0)
        return True
