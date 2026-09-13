"""M3-DSH 实验 F: 负费率 Carry + 急跌反弹 (同时吃「反弹」与「收费率」).

为什么是这两个条件的组合(全部基于本项目实测数据, 非形态猜测):
    ① 急跌后反弹有正期望:
         实测「过去 15 分钟跌超 5%」桶的未来 60 分钟收益 +2.379%(胜率 58.2%, n=201)
         实验 B(纯急跌反弹, 无费率条件)已交出 2 笔全胜(+4.42% / +3.09%)的初步结果
    ② 负费率提供持有期现金流:
         实测「年化费率 < -7%」桶在 15/60/240 分钟三个尺度上都是最好的桶之一
         持有负费率多头时**空头付钱给多头**, 等于为「等待反弹」这件事付补贴
    ③ 组合优势: B 的弱点是需要快速反弹(时间成本), 而 F 在等待期间还能收钱,
       因此在同样的入场判断下, F 的持有成本更低、可容忍的等待更久。

与其它实验的关系:
    A 追涨(已停用) / B 急跌反弹(无补贴) / C 负费率长持(等趋势) / D 压缩突破 / E 费率极值做空
    F = B 与 C 的交集: 用 B 的入场时机 + C 的现金流条件

风控: 与全部实验共用 stops_core(四不变量: 距离钳制/峰值单调收紧/风险上限/权益风险=d×杠杆)。
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

log = logging.getLogger("freqtrade.M3CarryDip")
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"


def _utc(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


class M3CarryDip(IStrategy):
    """负费率 Carry + 急跌反弹: 收着补贴买下跌."""

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

    # ---- 核心入场: 急跌 + 负费率(收钱) ----
    # 门槛依据(2026-09-13 统一口径检验, 34 小时 / 526 合约 / 1 分钟粒度):
    #   15m<=-2.5% -> 去极值均值 +0.085%, 胜率 54.7%, 时段 3/4   (原设置)
    #   15m<=-3.5% -> 去极值均值 +0.535%, 胜率 59.9%, 时段 4/4   <- 明显更好
    #   15m<=-5%   -> 去极值均值 +1.667%, 胜率 66.6%, 时段 4/4   (最强但样本更少)
    # 因此把门槛从 -2.5% 收紧到 -3.5%, 在信号数量与质量之间取平衡。
    DIP_15M_PCT = -3.5         # 近 15 分钟跌幅(经稳健性检验的最优折中)
    DIP_1H_PCT = -5.0          # 近 1 小时跌幅
    MAX_FUNDING_ANN = -0.15    # 年化费率必须 <= -15%(持有期间收费率)
    MAX_CHANGE_24H = 60.0      # 排除已暴涨的标的(避免接在顶部)
    MIN_QUOTE_VOL = 15_000_000.0
    MAX_SPREAD_BPS = 15.0
    RSI5_FLOOR = 10.0          # 5m RSI 地板(低于此值通常是崩盘)
    RSI5_MAX = 52.0            # 仍在弱势区才买(不追已反弹完的)
    MAX_RSI4 = 78.0            # 4h 不能极端超买

    # ---- 持有设计: 等反弹 + 收补贴 ----
    MAX_HOLD_HOURS = 24
    FUNDING_FLIP_ANN = 0.05    # 费率转正且超过该值 -> 补贴消失, 逻辑失效
    TRAIL_START_PROFIT = 0.025
    TRAIL_ATR_MULT = 2.4
    PROFIT_PROTECT = 0.55
    PARTIAL_TIERS = (0.06, 0.15)
    PARTIAL_RATIO = 0.35

    # ---- 风控 ----
    leverage_value = 2.0       # 抄底用低杠杆
    max_leverage = 2.0
    STOP_ATR_MULT = 2.2
    HARD_STOP = -0.055
    RISK_BUDGET = 0.007
    RISK_CEILING = 0.010
    MIN_STAKE_RATIO = 0.04
    MAX_STAKE_RATIO = 0.28
    TARGET_STAKE_RATIO = 0.18
    REENTRY_COOLDOWN_MIN = 30

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig: dict[str, int] = {}
        log.info("[F] 负费率 Carry + 急跌反弹启动 (%s)", _utc(int(time.time() * 1000)))
        log.info("[F] 门槛: 15m<=%.1f%% 或 1h<=%.1f%% | 年化费率<=%.0f%% | 24h涨幅<=%.0f%% | "
                 "杠杆%.1fx 风险预算%.1f%%/上限%.1f%% 最长%d小时",
                 self.DIP_15M_PCT, self.DIP_1H_PCT, self.MAX_FUNDING_ANN * 100,
                 self.MAX_CHANGE_24H, self.leverage_value, self.RISK_BUDGET * 100,
                 self.RISK_CEILING * 100, self.MAX_HOLD_HOURS)

    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        # 不接飞刀: 价格不得低于 1h EMA50 的 96%(否则视为趋势性下跌而非急跌)
        dataframe["safe"] = (dataframe["close"] > dataframe["ema50"] * 0.96).astype(float)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["mom_15m"] = dataframe["close"].pct_change(3) * 100.0
        dataframe["mom_1h"] = dataframe["close"].pct_change(12) * 100.0
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
        safe1h = dataframe.get("safe_1h", pd.Series(1.0, index=dataframe.index)).fillna(1.0)
        rsi4 = dataframe.get("rsi_1h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)

        dip = (dataframe["mom_15m"] <= self.DIP_15M_PCT) | (dataframe["mom_1h"] <= self.DIP_1H_PCT)
        # 弱确认: 不在当根新低, 或放量(恐慌盘)
        confirm = ((dataframe["close"] > dataframe["low_3"] * 1.002)
                   | (dataframe["vol_ratio"] > 1.4))
        ok = (dataframe["m3_in_pool"].astype(bool)
              & dip                                                    # 急跌
              & (dataframe["m3_funding_ann"] <= self.MAX_FUNDING_ANN)   # 负费率(收补贴)
              & (dataframe["m3_change_24h"] <= self.MAX_CHANGE_24H)
              & (dataframe["m3_vol"] >= self.MIN_QUOTE_VOL)
              & (dataframe["m3_spread"] <= self.MAX_SPREAD_BPS)
              & confirm
              & (safe1h >= 0.5)                                        # 1h 未破位
              & (dataframe["rsi"] >= self.RSI5_FLOOR)
              & (dataframe["rsi"] <= self.RSI5_MAX)
              & (rsi4 <= self.MAX_RSI4)
              & (dataframe["m3_oi"] > -15.0)
              & (dataframe["volume"] > 0))
        dataframe.loc[ok, ["enter_long", "enter_tag"]] = (1, "carry_dip")

        i = len(dataframe) - 1
        st = self._sig
        st["n"] = st.get("n", 0) + 1
        st["sig"] = st.get("sig", 0) + int(bool(ok.iloc[i]))
        gates = {
            "pool": bool(dataframe["m3_in_pool"].iloc[i]),
            "dip": bool(dip.iloc[i]),
            "neg_funding": bool(dataframe["m3_funding_ann"].iloc[i] <= self.MAX_FUNDING_ANN),
            "chg24": bool(dataframe["m3_change_24h"].iloc[i] <= self.MAX_CHANGE_24H),
            "liq": bool((dataframe["m3_vol"].iloc[i] or 0) >= self.MIN_QUOTE_VOL),
            "safe1h": bool(safe1h.iloc[i] >= 0.5),
            "rsi_band": bool(self.RSI5_FLOOR <= (dataframe["rsi"].iloc[i] or 0) <= self.RSI5_MAX),
            "confirm": bool(confirm.iloc[i]),
        }
        acc = st.setdefault("g", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["n"] % 100 == 0:
            log.info("[F] 评估 %d 次, 信号 %d 次 | 门槛通过率: %s", st["n"], st["sig"],
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
            mom = float(row.get("mom_15m", 0.0) or 0.0)
            # 费率越负(补贴越厚) + 跌得越急(反弹空间越大) -> 越值得下注
            conf = 1.0
            if ann <= -0.60:
                conf += 0.20
            elif ann <= -0.30:
                conf += 0.10
            if mom <= -4.0:
                conf += 0.15
            elif mom <= -2.5:
                conf += 0.05
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=sd, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=ann)
            if not plan.ok:
                log.warning("[F] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            if min_stake and stake < min_stake:
                stake = min(min_stake, max_stake * 0.9 if max_stake else min_stake)
            self._diag[pair] = {"stop_dist": sd, "leverage": float(leverage),
                                "stake": float(stake), "design_risk": float(plan.risk_pct),
                                "ann": ann, "mom": mom}
            log.info("[F] %s 仓位: 费率年化%.0f%% 15m动量%.2f%% 止损距离%.2f%% 杠杆%.1f -> "
                     "保证金%.2f (名义%.2f, 设计风险%.2f%%) [%s]", pair, ann * 100, mom, sd * 100,
                     leverage, stake, stake * leverage, plan.risk_pct * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[F] 仓位异常 %s: %s", pair, exc)
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
        log.info("[F] %s 阶梯止盈第%d档: 浮盈%.2f%% 减%.2f", trade.pair, n + 1,
                 current_profit * 100, amount)
        return -float(amount)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        """离场: 补贴消失 或 反弹到位 或 超时."""
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0) if row is not None else 0.0
            rsi5 = float(row.get("rsi", 50.0) or 50.0) if row is not None else 50.0
        except Exception:  # noqa: BLE001
            ann, rsi5 = 0.0, 50.0
        # 费率转正且明显 -> 不再收补贴, 策略前提消失
        if ann >= self.FUNDING_FLIP_ANN and current_profit < 0.01:
            return "carry_flipped"
        # 反弹到位: RSI 回到中性且已有浮盈
        if rsi5 >= 58 and current_profit > 0.02:
            return "dip_recovered"
        if hold_h > self.MAX_HOLD_HOURS:
            return "carry_dip_timeout"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[F][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 入场费率年化%.0f%% "
                     "15m动量%.2f%% | 资金费%+.4f | 持有%.1f小时", pair, exit_reason,
                     d["design_risk"] * 100, ratio * 100, d["ann"] * 100, d.get("mom", 0),
                     trade.funding_fees or 0.0,
                     (current_time - trade.open_date_utc).total_seconds() / 3600.0)
        return True
