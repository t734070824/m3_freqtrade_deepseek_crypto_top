"""M3-DSH 实验 G: B+F 融合 —— 负费率 + 急跌, 更快进出.

为什么做这个融合(2026-09-13 用 34 小时真实数据完成的条件检验):
    唯一反复通过稳健性检验的方向是「急跌买入」, 而"再加一层负费率"能进一步提高期望:

        条件(均含正确 15 分钟窗口)          样本    去极值均值    胜率
        B: 15m<=-3.5% (无费率要求)         1320    +0.528%     59.9%
        F: 负费率 + 15m<=-3.5%              298    +0.525%     53.0%
        F: 负费率 + 1h<=-5%                 515    +1.243%     56.3%   <- 期望最高
        C: 负费率 + 24h 5~60% (趋势票)       9472    -0.160%     48.9%   <- 不成立

    同时实测: 该方向的收益**随时间快速衰减**(未来 60 分钟口径),
    因此本策略刻意采用**更短的持有设计**, 而不是 F 的长持(最长 24 小时):
        * 止盈更近(阶梯 +3.5% / +8%), 反弹到位即收
        * 超时更短(最长 6 小时, F 是 24 小时)
        * 放弃"等资金费结算"这一诉求 —— 实测费率只占 F 收益的 4.6%,
          不值得为了收一次费率而把仓位暴露几小时

与已有实验的区别:
    B  = 急跌反弹(不要求费率)         —— 本策略的第一个门槛
    F  = 负费率 + 急跌, 长持收费率    —— 本策略取其前两个门槛, 但改成短持
    G  = 负费率 + 急跌 + 短持快收      —— 目标是把「反弹 + 补贴」快速兑现
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

log = logging.getLogger("freqtrade.M3CarryDipTurbo")
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"


class M3CarryDipTurbo(IStrategy):
    """B+F 融合: 负费率 + 急跌, 短持快收."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    ignore_roi_if_entry_signal = True
    startup_candle_count = 200

    use_custom_stoploss = True
    position_adjustment_enable = True
    stoploss = -0.15
    minimal_roi = {}

    # ---- 入场: 与经验证的两个条件完全对齐 ----
    DIP_15M_PCT = -3.5          # 15 分钟跌超 3.5%(B 的门槛, 去极值 +0.528%)
    DIP_1H_PCT = -5.0           # 1 小时跌超 5%(F 的门槛, 去极值 +1.243%)
    MAX_FUNDING_ANN = -0.15     # 年化费率 <= -15%(持有收补贴)
    MAX_CHANGE_24H = 60.0
    MIN_QUOTE_VOL = 15_000_000.0
    MAX_SPREAD_BPS = 15.0
    RSI5_FLOOR = 10.0
    RSI5_MAX = 52.0
    MAX_RSI4 = 78.0
    MIN_OI_CHG = -15.0

    # ---- 持有: 短持快收(实测收益随时间快速衰减) ----
    MAX_HOLD_HOURS = 6
    TRAIL_START_PROFIT = 0.02
    TRAIL_ATR_MULT = 2.0
    PROFIT_PROTECT = 0.6
    PARTIAL_TIERS = (0.035, 0.08)   # 比 F 更近: 3.5% / 8%
    PARTIAL_RATIO = 0.40            # 每次减仓更狠, 快速兑现
    FUNDING_FLIP_ANN = 0.05         # 费率转正且 > +5% 且无浮盈 -> 补贴消失即撤

    # ---- 风控 ----
    leverage_value = 2.0
    max_leverage = 2.0
    STOP_ATR_MULT = 2.0
    HARD_STOP = -0.05
    RISK_BUDGET = 0.007
    RISK_CEILING = 0.010
    MIN_STAKE_RATIO = 0.03   # 名义口径的单笔下限(交易所最小成交额); 必须 <= NOTIONAL_CAP/max_open_trades(修正1的兼容条件)
    MAX_STAKE_RATIO = 0.28
    TARGET_STAKE_RATIO = 0.18
    # 2026-09-14 根因修正: 单笔权益损失 = 止损距离 x 杠杆, 与仓位反推公式无关。
    # 名义敞口按权益比重封顶, 并按同时在持仓数做组合等风险分配(修正 1 + 3)。
    NOTIONAL_CAP = 0.30
    REENTRY_COOLDOWN_MIN = 20

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig: dict[str, int] = {}
        log.info("[G] B+F 融合策略启动 (%s UTC)",
                 datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        log.info("[G] 门槛: 15m<=%.1f%% 或 1h<=%.1f%% | 年化费率<=%.0f%% | 阶梯止盈 +%.1f%%/+%.1f%% "
                 "| 最长 %d 小时 | 杠杆 %.1fx 风险上限 %.1f%%",
                 self.DIP_15M_PCT, self.DIP_1H_PCT, self.MAX_FUNDING_ANN * 100,
                 self.PARTIAL_TIERS[0] * 100, self.PARTIAL_TIERS[1] * 100,
                 self.MAX_HOLD_HOURS, self.leverage_value, self.RISK_CEILING * 100)

    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["safe"] = (dataframe["close"] > dataframe["ema50"] * 0.96).astype(float)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["mom_15m"] = dataframe["close"].pct_change(3) * 100.0   # 15 分钟(5m×3)
        dataframe["mom_1h"] = dataframe["close"].pct_change(12) * 100.0   # 1 小时
        dataframe["low_3"] = dataframe["low"].rolling(3).min()
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, float("nan"))
        self._attach(dataframe, metadata)
        return dataframe

    def _attach(self, dataframe: DataFrame, metadata: dict) -> None:
        pair = metadata.get("pair", "")
        sym = pair.split("/")[0] + "USDT" if pair else ""
        self._refresh()
        wl = self._wl.get(sym, {})
        dataframe["m3_funding_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)
        dataframe["m3_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_spread"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_oi"] = float(wl.get("oi_chg_1h", 0.0) or 0.0)
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

    # ------------------------------------------------------------ 入场
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        safe1h = dataframe.get("safe_1h", dataframe.get("safe", None))
        if safe1h is None:
            import pandas as _pd
            safe1h = _pd.Series(1.0, index=dataframe.index)
        safe1h = safe1h.fillna(1.0)
        rsi4 = dataframe.get("rsi_1h", dataframe.get("rsi", None))
        if rsi4 is None:
            import pandas as _pd
            rsi4 = _pd.Series(50.0, index=dataframe.index)
        rsi4 = rsi4.fillna(50.0)

        dip15 = dataframe["mom_15m"] <= self.DIP_15M_PCT
        dip1h = dataframe["mom_1h"] <= self.DIP_1H_PCT
        confirm = ((dataframe["close"] > dataframe["low_3"] * 1.002)
                   | (dataframe["vol_ratio"] > 1.4))
        ok = (dataframe["m3_in_pool"].astype(bool)
              & (dip15 | dip1h)                                        # 急跌(B 与 F 的门槛)
              & (dataframe["m3_funding_ann"] <= self.MAX_FUNDING_ANN)   # 负费率(收补贴)
              & (dataframe["m3_change_24h"] <= self.MAX_CHANGE_24H)
              & (dataframe["m3_vol"] >= self.MIN_QUOTE_VOL)
              & (dataframe["m3_spread"] <= self.MAX_SPREAD_BPS)
              & confirm
              & (safe1h >= 0.5)
              & (dataframe["rsi"] >= self.RSI5_FLOOR)
              & (dataframe["rsi"] <= self.RSI5_MAX)
              & (rsi4 <= self.MAX_RSI4)
              & (dataframe["m3_oi"] > self.MIN_OI_CHG)
              & (dataframe["volume"] > 0))
        dataframe.loc[ok, ["enter_long", "enter_tag"]] = (1, "carry_dip_turbo")

        i = len(dataframe) - 1
        st = self._sig
        st["n"] = st.get("n", 0) + 1
        st["sig"] = st.get("sig", 0) + int(bool(ok.iloc[i]))
        gates = {
            "pool": bool(dataframe["m3_in_pool"].iloc[i]),
            "dip15": bool(dip15.iloc[i]),
            "dip1h": bool(dip1h.iloc[i]),
            "negf": bool(dataframe["m3_funding_ann"].iloc[i] <= self.MAX_FUNDING_ANN),
            "liq": bool((dataframe["m3_vol"].iloc[i] or 0) >= self.MIN_QUOTE_VOL),
            "safe1h": bool(safe1h.iloc[i] >= 0.5),
            "confirm": bool(confirm.iloc[i]),
        }
        acc = st.setdefault("g", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["n"] % 100 == 0:
            log.info("[G] 评估 %d 次, 信号 %d 次 | 门槛通过率: %s", st["n"], st["sig"],
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
            m15 = float(row.get("mom_15m", 0.0) or 0.0)
            m60 = float(row.get("mom_1h", 0.0) or 0.0)
            # 1 小时级别的急跌(去极值 +1.243%)比 15 分钟级别(+0.525%)期望更高 -> 给更多权重
            conf = 1.0
            if m60 <= self.DIP_1H_PCT:
                conf += 0.25
            if m15 <= self.DIP_15M_PCT:
                conf += 0.10
            if ann <= -0.60:
                conf += 0.15
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=sd, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=ann, notional_cap=self.NOTIONAL_CAP,
                                 open_trades=len(Trade.get_open_trades()),
                                 max_open_trades=self.config.get("max_open_trades", 1))
            if not plan.ok:
                log.warning("[G] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            # 2026-09-14 修正: 原实现在 stake < min_stake 时把仓位顶回 min_stake, 这会
            # **绕过 risk_ceiling 与名义敞口封顶** —— 典型的「为了能成交而放大仓位」。
            if min_stake and stake < min_stake:
                log.info("[G] %s 跳过: 计算仓位 %.2f 低于交易所最小 %.2f, 不为凑单放大仓位",
                         pair, stake, min_stake)
                return 0.0
            self._diag[pair] = {"stop_dist": sd, "leverage": float(leverage),
                                "stake": float(stake), "design_risk": float(plan.risk_pct),
                                "notional_pct": float(plan.notional_pct),
                                "ann": ann, "m15": m15, "m60": m60}
            log.info("[G] %s 仓位: 费率年化%.0f%% 15m%.2f%% 1h%.2f%% 止损距离%.2f%% 杠杆%.1f -> "
                     "保证金%.2f (名义%.2f=权益%.1f%%, 止损一发风险%.2f%%) [%s]", pair, ann * 100,
                     m15, m60, sd * 100, leverage, stake, stake * leverage,
                     plan.notional_pct * 100, plan.risk_pct * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[G] 仓位异常 %s: %s", pair, exc)
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
        log.info("[G] %s 阶梯止盈第%d档(浮盈%.2f%% >= %.1f%%): 减仓 %.2f",
                 trade.pair, n + 1, current_profit * 100, self.PARTIAL_TIERS[n] * 100, amount)
        return -float(amount)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        """短持快收: 反弹到位/R针修复即走; 补贴消失也走; 超时更短."""
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0) if row is not None else 0.0
            rsi5 = float(row.get("rsi", 50.0) or 50.0) if row is not None else 50.0
        except Exception:  # noqa: BLE001
            ann, rsi5 = 0.0, 50.0
        if ann >= self.FUNDING_FLIP_ANN and current_profit < 0.01:
            return "carry_flipped"
        if rsi5 >= 56 and current_profit > 0.012:
            return "dip_recovered_fast"      # 比 F 更早收割(56 vs 58, 1.2% vs 2%)
        if hold_h > self.MAX_HOLD_HOURS:
            return "turbo_timeout"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[G][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 入场 费率%.0f%% 15m%.2f%% "
                     "1h%.2f%% | 资金费%+.4f | 持有%.1f小时", pair, exit_reason,
                     d["design_risk"] * 100, ratio * 100, d["ann"] * 100, d.get("m15", 0),
                     d.get("m60", 0), trade.funding_fees or 0.0,
                     (current_time - trade.open_date_utc).total_seconds() / 3600.0)
        return True
