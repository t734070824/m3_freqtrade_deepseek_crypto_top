"""M3-DSH 实验 E: 费率极值反转做空 (crowded-long unwind).

假设(核心经济逻辑, 不是形态猜测):
    当某合约的资金费率处于其**自身历史极值**时, 说明多头拥挤到愿意支付极高成本持仓。
    此时做空有两重收益:
      ① 资金费率本身: 每期结算**多头付钱给空头**(直接现金流);
      ② 拥挤回补: 杠杆多头一旦被迫平仓, 价格会迅速回落。
    因此「极高费率 + 价格极度拉升」是高胜率、且有正现金流的做空位置。

为什么用「自适应分位」而不是绝对阈值(2026-09-12 实测教训):
    费率水平随市场大幅漂移。实测当天全体候选的最大年化仅 **5.5%**,
    而个别标的的历史极值可达 +200% 以上 —— 若把门槛写死成「年化 >= 100%」,
    在费率平淡期将永不触发; 反之在费率普涨期又形同虚设。
    用「该币自身历史分位(>= P90)」可自适应任何市场环境。
    分位数由采集器写入 watchlist.json 的 funding_pct_rank 字段。

风控: 与其它实验共用 stops_core(四不变量)。做空额外注意:
    - 杠杆压到 2x(空头挤压风险高于多头回撤)
    - 有硬性时间上限(费率逻辑失效就撤)
    - 费率回落即离场(收益来源消失)
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

log = logging.getLogger("freqtrade.M3FundingShort")
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"


class M3FundingShort(IStrategy):
    """费率极值反转做空: 收多头拥挤的费率 + 做多头的被迫平仓."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True           # 只做空: 多头侧由 A/C 承担
    process_only_new_candles = True
    use_exit_signal = True
    ignore_roi_if_entry_signal = True
    startup_candle_count = 200

    use_custom_stoploss = True
    position_adjustment_enable = True
    stoploss = -0.15
    minimal_roi = {}

    # ---- 入场: 费率极值(自适应分位) + 价格极度拉升 + 拥挤度 ----
    MIN_PCT_RANK = 0.90            # 当前费率在该币自身历史的 >= P90
    MIN_SAMPLES = 30               # 历史样本不足时不使用分位(改用绝对阈值回退)
    ABS_FALLBACK_ANN = 0.20        # 样本不足时的绝对阈值(年化 >= 20%)
    MIN_FUNDING_POSITIVE = 0.0     # 必须是正费率(空头收费率)
    MIN_CHANGE_24H = 25.0          # 价格必须已大幅拉升(才有回补空间)
    MAX_CHANGE_24H = 400.0         # 排除极端异常值
    MIN_QUOTE_VOL = 20_000_000.0
    MAX_SPREAD_BPS = 20.0
    MIN_RSI4 = 70.0                # 4h 超买
    MAX_CROWD_RATIO = 1.35         # 大户持仓多空比不过度看空(避免空头自己拥挤)
    MAX_HOLD_HOURS = 36

    # ---- 出场: 费率回落 或 技术回归 ----
    FUNDING_EXIT_RATIO = 0.5       # 当前费率跌破入场时的 50% -> 收益来源消失
    PCT_RANK_EXIT = 0.55           # 分位回落到 0.55 以下 -> 拥挤已释放

    # ---- 局部止盈 ----
    PARTIAL_TIERS = (0.08, 0.20)
    PARTIAL_RATIO = 0.35

    # ---- 风控(空头更保守) ----
    leverage_value = 2.0
    max_leverage = 2.0
    STOP_ATR_MULT = 2.0
    TRAIL_START_PROFIT = 0.03
    TRAIL_ATR_MULT = 2.2
    HARD_STOP = -0.06
    PROFIT_PROTECT = 0.55
    RISK_BUDGET = 0.007
    RISK_CEILING = 0.010
    MIN_STAKE_RATIO = 0.04
    MAX_STAKE_RATIO = 0.25
    TARGET_STAKE_RATIO = 0.18
    REENTRY_COOLDOWN_MIN = 45

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig: dict[str, int] = {}
        log.info("[FSHORT] 费率极值反转做空启动 (%s UTC)",
                 datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        log.info("[FSHORT] 门槛: 费率分位>=%.2f(样本>=%d, 否则年化>=%.0f%%) 24h涨幅>=%.0f%% "
                 "4hRSI>=%.0f 杠杆%.1fx 风险预算%.1f%%/上限%.1f%% 最长%d小时",
                 self.MIN_PCT_RANK, self.MIN_SAMPLES, self.ABS_FALLBACK_ANN * 100,
                 self.MIN_CHANGE_24H, self.MIN_RSI4, self.leverage_value,
                 self.RISK_BUDGET * 100, self.RISK_CEILING * 100, self.MAX_HOLD_HOURS)

    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        # 空头只做「上涨结构尚未破坏但已极端拉伸」的标的, 不追空下跌趋势
        dataframe["up"] = ((dataframe["ema21"] > dataframe["ema50"]) |
                           (dataframe["close"] > dataframe["ema50"])).astype(float)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["ext"] = (dataframe["close"] - dataframe["ema21"]) / dataframe["ema21"] * 100.0
        self._attach(dataframe, metadata)
        return dataframe

    def _attach(self, dataframe: DataFrame, metadata: dict) -> None:
        pair = metadata.get("pair", "")
        sym = pair.split("/")[0] + "USDT" if pair else ""
        self._refresh()
        wl = self._wl.get(sym, {})
        dataframe["m3_funding_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_pct_rank"] = float(wl.get("funding_pct_rank", 0.0) or 0.0)
        dataframe["m3_fsamples"] = float(wl.get("funding_samples", 0) or 0)
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)
        dataframe["m3_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_spread"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_ls"] = float(wl.get("ls_ratio", 0.0) or 0.0)
        dataframe["m3_score"] = float(wl.get("score", 0.0) or 0.0)
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
        up1h = dataframe.get("up_1h", pd.Series(1.0, index=dataframe.index)).fillna(1.0)
        rsi4 = dataframe.get("rsi_1h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)

        positive = dataframe["m3_funding_ann"] > self.MIN_FUNDING_POSITIVE
        # 有足够历史时用分位; 否则回退到绝对阈值
        has_hist = dataframe["m3_fsamples"] >= self.MIN_SAMPLES
        extreme = ((has_hist & (dataframe["m3_pct_rank"] >= self.MIN_PCT_RANK))
                   | (~has_hist & (dataframe["m3_funding_ann"] >= self.ABS_FALLBACK_ANN)))

        ok = (dataframe["m3_in_pool"].astype(bool)
              & positive & extreme                                        # 核心: 费率极值
              & (dataframe["m3_change_24h"] >= self.MIN_CHANGE_24H)        # 价格已大幅拉升
              & (dataframe["m3_change_24h"] <= self.MAX_CHANGE_24H)
              & (dataframe["m3_vol"] >= self.MIN_QUOTE_VOL)
              & (dataframe["m3_spread"] <= self.MAX_SPREAD_BPS)
              & (rsi4 >= self.MIN_RSI4)                                    # 4h 超买
              & (up1h >= 0.5)                                              # 不是下跌趋势里追空
              & ((dataframe["m3_ls"] <= self.MAX_CROWD_RATIO)
                 | (dataframe["m3_ls"] == 0.0))                            # 大户未极端看空
              & (dataframe["ext"] > 0)                                     # 价格在均线上方(拉伸)
              & (dataframe["volume"] > 0))
        # 触发: 动能转头 / RSI 下穿 / 跌破短均线
        trig = ((dataframe["macdhist"] < 0) & (dataframe["macdhist"].shift(1) >= 0)) \
               | ((dataframe["rsi"] < 50) & (dataframe["rsi"].shift(1) >= 50)) \
               | ((dataframe["close"] < dataframe["ema21"])
                  & (dataframe["close"].shift(1) >= dataframe["ema21"].shift(1)))
        dataframe.loc[ok & trig, ["enter_short", "enter_tag"]] = (1, "funding_short")

        i = len(dataframe) - 1
        st = self._sig
        st["n"] = st.get("n", 0) + 1
        st["sig"] = st.get("sig", 0) + int(bool((ok & trig).iloc[i]))
        gates = {
            "pool": bool(dataframe["m3_in_pool"].iloc[i]),
            "positive": bool(positive.iloc[i]),
            "extreme": bool(extreme.iloc[i]),
            "chg24": bool(dataframe["m3_change_24h"].iloc[i] >= self.MIN_CHANGE_24H),
            "rsi4_ob": bool(rsi4.iloc[i] >= self.MIN_RSI4),
            "ext_pos": bool(dataframe["ext"].iloc[i] > 0),
            "liq": bool((dataframe["m3_vol"].iloc[i] or 0) >= self.MIN_QUOTE_VOL),
            "trigger": bool(trig.iloc[i]),
        }
        acc = st.setdefault("g", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["n"] % 100 == 0:
            pool = max(st.get("pool_n", 0), 1)
            log.info("[FSHORT] 评估 %d 次, 信号 %d 次 | 门槛通过率: %s", st["n"], st["sig"],
                     "  ".join("%s=%.0f%%" % (k, acc.get(k, 0) / st["n"] * 100) for k in gates))
        if gates["pool"]:
            st["pool_n"] = st.get("pool_n", 0) + 1
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
            pr = float(row.get("m3_pct_rank", 0.0) or 0.0)
            # 费率越极端(分位越高、绝对年化越大), 做空的现金流越厚 -> 加仓
            conf = 1.0
            if ann >= 0.60 or pr >= 0.97:
                conf = 1.35
            elif ann >= 0.30 or pr >= 0.94:
                conf = 1.20
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=sd, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=ann)
            if not plan.ok:
                log.warning("[FSHORT] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            if min_stake and stake < min_stake:
                stake = min(min_stake, max_stake * 0.9 if max_stake else min_stake)
            self._diag[pair] = {"stop_dist": sd, "leverage": float(leverage),
                                "stake": float(stake), "design_risk": float(plan.risk_pct),
                                "ann": ann, "pr": pr}
            log.info("[FSHORT] %s 做空仓位: 费率年化%.0f%%(分位%.2f) 止损距离%.2f%% 杠杆%.1f -> "
                     "保证金%.2f (名义%.2f, 设计风险%.2f%%) [%s]", pair, ann * 100, pr,
                     sd * 100, leverage, stake, stake * leverage, plan.risk_pct * 100,
                     plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[FSHORT] 仓位异常 %s: %s", pair, exc)
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
        is_short = bool(trade.is_short)
        # 峰值浮盈(空头: 价格越低越有利)
        peak = current_profit
        try:
            if trade.min_rate and is_short:
                peak = (trade.open_rate - trade.min_rate) / trade.open_rate * lev
        except Exception:  # noqa: BLE001
            pass
        p = StopParams(stop_atr_mult=self.STOP_ATR_MULT, trail_atr_mult=self.TRAIL_ATR_MULT,
                       trail_start_profit=self.TRAIL_START_PROFIT, hard_stop=self.HARD_STOP,
                       profit_protect=self.PROFIT_PROTECT)
        d, _ = stop_price_distance(current_profit, lev, atr, p, peak_profit=peak)
        sp = stop_rate(current_rate, d, is_short)
        val = freqtrade_stoploss_value(current_rate, sp, lev, is_short)
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
        log.info("[FSHORT] %s 止盈减仓: 浮盈%.2f%% 减%.2f", trade.pair, current_profit * 100, amount)
        return -float(amount)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        """离场核心: 费率逻辑消失即撤(这是本策略的收益来源)."""
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0) if row is not None else 0.0
            pr = float(row.get("m3_pct_rank", 0.0) or 0.0) if row is not None else 0.0
            rsi4 = float(row.get("rsi_1h", 50.0) or 50.0) if row is not None else 50.0
        except Exception:  # noqa: BLE001
            ann, pr, rsi4 = 0.0, 0.0, 50.0

        d = self._diag.get(pair)
        base = d["ann"] if d else 0.0
        if ann < 0 or (base > 0 and ann <= base * self.FUNDING_EXIT_RATIO):
            return "funding_cooled"          # 费率回落 -> 收益来源消失
        if pr and pr <= self.PCT_RANK_EXIT:
            return "crowding_released"       # 拥挤已释放
        if rsi4 <= 55 and current_profit > 0.01:
            return "rsi_normalized"          # 超买修复完成且有浮盈
        if hold_h > self.MAX_HOLD_HOURS:
            return "funding_short_timeout"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[FSHORT][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 入场费率年化%.0f%%"
                     "(分位%.2f) | 资金费%+.4f | 持有%.1f小时", pair, exit_reason,
                     d["design_risk"] * 100, ratio * 100, d["ann"] * 100, d["pr"],
                     trade.funding_fees or 0.0,
                     (current_time - trade.open_date_utc).total_seconds() / 3600.0)
        return True
