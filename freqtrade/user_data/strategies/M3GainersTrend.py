"""M3-DSH 涨幅榜多空趋势跟随策略 (策略类名见文件内 class 定义).

提示: freqtrade 的 IResolver 用「源码中是否出现 "class <名字>(" 做快速筛选, 若本
docstring 里再写一次类名会被误判为重复策略(DUPLICATE NAME), 故此处不重复类名。

设计目标
--------
在 Binance USDT-M 合约「涨幅榜」标的池中, 顺着主趋势持续参与、持续持有、持续收割:

* 主趋势判定 : 4h 均线结构 + 1h 相位 + 5m 触发 (三周期共振)
* 动态选币   : 读取采集器产出的 watchlist.json (涨幅榜 + 资金费率 + OI + 多空比 + 恐贪 打分)
* 资金费率   : **必须考虑** —— 持仓成本/补贴直接进入开仓门槛、仓位规模与持仓时长决策
* 双向交易   : 多头(吃趋势+负费率补贴) / 空头(吃下跌+正费率补贴)
* 持续持有   : 宽幅 ATR 跟踪止盈 + 趋势未破坏不轻易离场, 分批收割而非一次性平仓
* 风控       : 单笔风险预算 + 组合敞口上限 + 极端费率/拥挤度禁入

时间约定: 本文件所有时间均为 UTC 毫秒时间戳; 日志/展示处显式标注时区。

容器环境: 采集器数据挂载在 /workspace/data, 本策略只读。
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative

# 风控纯函数核心(stops_core.py 与本文件同目录) —— 数学与框架解耦, 单测见
# freqtrade/tests/unit/test_stops_core.py。
# 注意: freqtrade 用 importlib 按文件路径加载策略, 不会自动把策略目录加入 sys.path,
# 因此这里显式用 __file__ 推导目录(不能用相对路径, cwd 可能是任意位置)。
_STRAT_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_STRAT_DIR, os.path.dirname(_STRAT_DIR)):   # strategies/ 与 user_data/
    if _p not in sys.path:
        sys.path.insert(0, _p)
from stops_core import (DIST_MAX, DIST_MIN, StopParams,  # noqa: E402
                        entry_stop_distance, freqtrade_stoploss_value, leverage_for_risk,
                        plan_position, stop_price_distance, stop_rate)

log = logging.getLogger("freqtrade.M3GainersTrend")

# ---------------------------------------------------------------- 数据路径
DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"
MARKET_DB = DATA_DIR / "m3dsc_market.db"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


# =====================================================================
#  动态选币 pairlist: 读取采集器「涨幅榜打分」结果
#  (IResolver 要求类的 __module__ 等于所在文件名, 故必须定义在本模块顶层;
#   同时由 user_data/pairlists/M3GainersPairlist.py 提供独立的同名扩展点)
# =====================================================================
def load_watchlist_pairs(n: int = 80, *, max_age_s: float = 1800.0) -> list[str]:
    """把采集器的候选池转换成 freqtrade 合约代码 (BTCUSDT -> BTC/USDT:USDT).

    文件由采集器每分钟原子替换; 过期则抛错, 由调用方兜底。
    """
    raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    age_s = (time.time() * 1000 - int(raw.get("generated_ms", 0))) / 1000.0
    if age_s > max_age_s:
        raise RuntimeError(f"watchlist 已过期 {age_s:.0f}s (> {max_age_s:.0f}s)")
    pairs: list[str] = []
    for c in raw.get("candidates", []):
        sym = str(c.get("symbol", ""))
        if not sym.endswith("USDT"):
            continue
        pairs.append(f"{sym[:-4]}/USDT:USDT")
        if len(pairs) >= n:
            break
    return pairs


FALLBACK_PAIRS: list[str] = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "LTC/USDT:USDT", "TRX/USDT:USDT", "SUI/USDT:USDT",
]


# =====================================================================
#  策略主体
# =====================================================================
class M3GainersTrend(IStrategy):
    """涨幅榜三周期趋势跟随 (多空双向, 资金费率感知)."""

    INTERFACE_VERSION = 3

    # ---- freqtrade 基础 ----
    timeframe = "5m"
    can_short = True
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True
    startup_candle_count = 240

    # 关键开关 (2026.x: 必须显式打开, 否则回调不会被调用)
    use_custom_stoploss = True
    position_adjustment_enable = True

    # 兜底止损(freqtrade 会用它作为「最大亏损」硬上限):
    # 注意 futures 下该值是「相对权益的风险比例」, 实际价格距离 = |值| / 杠杆
    stoploss = -0.20

    # freqtrade 层保护
    minimal_roi = {}

    # ---- 仓位/杠杆 (合约) ----
    # 2026-09-12 复盘: 4x 杠杆 + 12% 价格止损 = 单笔 48% 权益敞口, 是「9 笔亏 -8%~-15% 权益」
    # 的直接来源。趋势策略的收益应主要来自**持有时间**, 而不是杠杆倍数, 故整体下调到 3x,
    # 并把止损价格距离硬钳制在 2%~8%(见 stops_core.DIST_MAX)。
    leverage_value = 3.0
    max_leverage = 3.0

    # ---- 风控参数 ----
    STOP_ATR_MULT = 2.2            # 初始止损 = 2.2 × ATR(5m), 距离钳制在 2%~8%
    # 捕获率优化(2026-09-12 实测): 盈利单的价格峰值中位只有 +1~2%, 而 2.6×ATR 的跟踪
    # 距离会把利润几乎全部回吐(实测只吃到峰值的 ~1/3)。改为「按利润分档收紧」,
    # 同时保留大行情不被过早打断(实测能抓到 +20% 级别的插针)。
    TRAIL_ATR_MULT = 2.6           # 跟踪距离基准
    TRAIL_TIGHT_1 = 1.6            # 浮盈 > 1.6×启动阈值 时收紧到 1.6×ATR
    TRAIL_TIGHT_2 = 1.0            # 浮盈 > 3×启动阈值   时收紧到 1.0×ATR
    TRAIL_START_PROFIT = 0.02      # 浮盈 2% 启动跟踪
    HARD_STOP = -0.070             # 权益硬止损 -7%
    MIN_LOCK_PROFIT = 0.008        # (保留: 权益口径的最小锁定利润)
    PROFIT_PROTECT = 0.7           # 盈利保护: 止损距离 <= 70% 的价格获利(至少锁定 30% 涨幅)
    RISK_BUDGET = 0.008            # 单笔风险预算: 止损触发时权益回撤目标 0.8%
    RISK_CEILING = 0.012           # 单笔风险硬上限: 任何情况下权益回撤 <= 1.2%
    MIN_STAKE_RATIO = 0.04         # 最小保证金比例
    MAX_STAKE_RATIO = 0.30         # 最大保证金比例
    TARGET_STAKE_RATIO = 0.20      # 目标保证金比例(用于反推杠杆安全上限)

    # ---- 「多头极端拥挤」反向做空路径 (涨幅榜特有的收割形态) ----
    SHORT_REV_SCORE = -35.0        # 打分必须强烈看空
    SHORT_REV_FUNDING_ANN = 0.80   # 年化费率 >= 80% 视为多头被榨到极致(空头同时收费率)
    SHORT_REV_RSI4 = 82.0          # 4h RSI 超买
    MIN_STICK = 0.25               # 最低榜单稳定性(过滤脉冲票; 0 = 尚未统计到, 放行)

    # ---- 动量甜区门槛 (2026-09-12 用 28 万条分钟样本实测) ----
    # 实测「24h 涨幅 -> 未来 60 分钟收益」的关系是非单调的:
    #   3~10%  -> -0.013% (胜率 48.6%)
    #   10~20% -> -0.312% (胜率 42.4%)  <-- 原策略大量入场区间, 期望为负
    #   20~40% -> +0.469% (胜率 48.0%)
    #   >40%   -> +4.506% (胜率 67.1%)  <-- 真正的动量延续区
    # 结论: 只在「已经大幅上涨且仍在延续」时做多; 中间地带属于均值回归区, 必须回避。
    MIN_CHANGE_24H = 20.0          # 24h 涨幅下限(避开 10~20% 的负期望区间)
    MIN_MOM_15M = 0.0              # 近 15 分钟动量下限(0 = 不要求加速, 但拒绝下跌)
    MOM_BARS_15M = 3               # 5m 周期下 3 根 = 15 分钟
    # 注意: 止损价格距离的钳制边界来自 stops_core.DIST_MIN / DIST_MAX(2% ~ 8%),
    # 不要在这里另写一套数字 —— 建仓与持仓期必须共用同一组边界。
    TAKE_PARTIAL_AT = 0.20         # 浮盈 20%(权益)才分批收割 —— 先让小利润奔跑
    PARTIAL_RATIO = 0.35           # 收割比例
    MAX_HOLD_HOURS = 240           # 最长持有 10 天(趋势票允许长时间持有)
    FUNDING_MAX_LONG_ANN = 0.45    # 做多年化费率上限(超过则不做多)
    FUNDING_MAX_SHORT_ANN = -0.50  # 做空年化费率下限
    FUNDING_EXIT_ANN = 0.90        # 持仓期间年化费率超过该值 -> 强制减仓

    # ---- 选币/择时阈值 ----
    # 入场门槛(2026-09-12 复盘上调): 原 12 分门槛下 1 小时尺度几乎无预测力
    # (Pearson 仅 +0.04), 提高到 20 分只做「多因子共振」的少数高质量机会。
    MIN_SCORE_LONG = 20.0
    MIN_SCORE_SHORT = -20.0
    MIN_QUOTE_VOL = 8_000_000.0    # 24h 成交额下限(USDT)
    # 防抖: 同一标的平仓后 N 分钟内不再开仓(避免被同一根插针反复收割)
    REENTRY_COOLDOWN_MIN = 20      # 冷却缩减: 60 分钟会错过同一趋势的二次入场

    plot_config = {
        "main_plot": {
            "ema_fast": {"color": "#2ecc71"},
            "ema_slow": {"color": "#3498db"},
            "ema_trend": {"color": "#9b59b6"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "#e67e22"}},
            "资金费率年化": {"funding_ann_pct": {"color": "#e74c3c"}},
            "M3打分": {"m3_score": {"color": "#1abc9c"}, "m3_oi_chg": {"color": "#f1c40f"}},
        },
    }

    # ---------------------------------------------------------------- 启动
    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms: int = 0
        self._macro: dict[str, float] = {}
        self._db_conn: sqlite3.Connection | None = None
        self._live_funding: dict[str, dict[str, float]] = {}
        # 防抖: 每个标的最近一次平仓时间 (UTC ms)
        self._last_exit: dict[str, int] = {}
        # 信号率统计(用于判断入场门槛是否过严)
        self._sig_stat: dict[str, int] = {"pairs": 0, "long": 0, "short": 0}
        # 风控埋点: 记录每笔交易开仓时的「设计风险」, 平仓时与**真实亏损**对照输出,
        # 用于检验「止损触发时权益回撤 <= 风险预算」这一不变量在实盘中是否真的成立。
        self._diag: dict[str, dict[str, float]] = {}
        log.info("[M3] 策略启动. 数据目录=%s, watchlist=%s", DATA_DIR, WATCHLIST_PATH)
        log.info("[M3] 风控参数: 初始止损=%.1f×ATR(距离钳制%.0f%%~%.0f%%) 跟踪=%.1f×ATR "
                 "跟踪启动浮盈=%.1f%% 硬止损=%.1f%% 分批收割=+%.0f%%/每档%.0f%% "
                 "单笔风险预算=%.1f%% 上限=%.1f%% 杠杆=%.1fx 费率上限 多%.0f%% 空%.0f%% 冷却=%d分钟 "
                 "打分门槛=%.0f",
                 self.STOP_ATR_MULT, DIST_MIN * 100, DIST_MAX * 100, self.TRAIL_ATR_MULT,
                 self.TRAIL_START_PROFIT * 100, self.HARD_STOP * 100,
                 self.TAKE_PARTIAL_AT * 100, self.PARTIAL_RATIO * 100,
                 self.RISK_BUDGET * 100, self.RISK_CEILING * 100, self.leverage_value,
                 self.FUNDING_MAX_LONG_ANN * 100, self.FUNDING_MAX_SHORT_ANN * 100,
                 self.REENTRY_COOLDOWN_MIN, self.MIN_SCORE_LONG)

    # ---------------------------------------------------------------- 配对/外部数据
    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(p, "1h") for p in pairs] + [(p, "4h") for p in pairs]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        # 主趋势方向: +1 上升, -1 下降, 0 震荡
        up = (dataframe["ema50"] > dataframe["ema200"]) & (dataframe["close"] > dataframe["ema50"])
        dn = (dataframe["ema50"] < dataframe["ema200"]) & (dataframe["close"] < dataframe["ema50"])
        dataframe["trend_dir"] = np.where(up, 1.0, np.where(dn, -1.0, 0.0))
        dataframe["trend_strength"] = np.where(
            dataframe["ema200"] > 0,
            (dataframe["ema50"] - dataframe["ema200"]) / dataframe["ema200"] * 100.0, 0.0)
        # 4h 量能确认: 成交量相对 20 周期均量的倍数(趋势需要量能支撑)
        dataframe["vol_sma_4h"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(24).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, np.nan)
        # 相位与延伸度
        dataframe["phase"] = np.where(
            (dataframe["close"] > dataframe["ema21"]) & (dataframe["ema21"] > dataframe["ema50"]), 1.0,
            np.where((dataframe["close"] < dataframe["ema21"]) & (dataframe["ema21"] < dataframe["ema50"]),
                     -1.0, 0.0))
        dataframe["ext_pct"] = (dataframe["close"] - dataframe["ema21"]) / dataframe["ema21"] * 100.0
        dataframe["macd"] = ta.MACD(dataframe)["macd"]
        dataframe["macdsignal"] = ta.MACD(dataframe)["macdsignal"]
        return dataframe

    # ---------------------------------------------------------------- 主图指标
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        bbands = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_width"] = (bbands["upperband"] - bbands["lowerband"]) / bbands["middleband"] * 100.0
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, np.nan)
        dataframe["ret_1h"] = dataframe["close"].pct_change(12) * 100.0
        # 近 15 分钟动量(5m 周期下 3 根 K 线): 用于「动量甜区」门槛与漏斗诊断
        dataframe["mom_15m"] = dataframe["close"].pct_change(self.MOM_BARS_15M) * 100.0
        dataframe["ret_4h"] = dataframe["close"].pct_change(48) * 100.0
        dataframe["hh20"] = dataframe["high"].rolling(20).max()
        dataframe["ll20"] = dataframe["low"].rolling(20).min()
        dataframe["breakout_up"] = dataframe["close"] >= dataframe["hh20"].shift(1)
        dataframe["breakout_dn"] = dataframe["close"] <= dataframe["ll20"].shift(1)
        # 近 3 根内的突破(不要求正好发生在当根): 解决「突破发生在上一根, 本轮错过入场」的问题。
        # 代价是入场价可能比突破点略差, 换来信号数量约翻倍。
        dataframe["breakout_up_3"] = (dataframe["close"].rolling(3).max()
                                      >= dataframe["hh20"].shift(3))
        dataframe["breakout_dn_3"] = (dataframe["close"].rolling(3).min()
                                      <= dataframe["ll20"].shift(3))

        # ---- 外部数据: 打分池 / 资金费率 / OI / 多空比 ----
        dataframe = self._attach_external(dataframe, metadata)
        return dataframe

    def _attach_external(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """把采集器的场内外数据按「K线时间」对齐到 dataframe (严格无未来函数)."""
        pair = metadata.get("pair", "")
        base = pair.split("/")[0] if pair else ""
        symbol = f"{base}USDT" if base else ""
        self._refresh_watchlist()

        wl = self._wl.get(symbol, {})
        dataframe["m3_score"] = float(wl.get("score", 0.0) or 0.0)
        sig = float(wl.get("oi_chg_1h", 0.0) or 0.0)
        dataframe["m3_oi_chg"] = sig
        dataframe["m3_ls_ratio"] = float(wl.get("ls_ratio", 0.0) or 0.0)
        dataframe["m3_taker"] = float(wl.get("taker_ratio", 1.0) or 1.0)
        dataframe["m3_spread_bps"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_rank"] = float(wl.get("rank_gain", 9999) or 9999)
        dataframe["m3_in_pool"] = bool(wl)
        dataframe["m3_quote_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_stick"] = float(wl.get("stability", 0.0) or 0.0)   # 榜单稳定性因子
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)  # 24h 涨幅(甜区门槛用)
        dataframe["m3_fng"] = float(self._macro.get("fear_greed", 50.0))

        # ---- 资金费率历史(严格无未来函数) ----
        # 结算资金费率「在结算时刻才生效」, 因此必须从上一根 K 线开始生效,
        # 否则用 fundingTime <= candle_time 会把未来信息带入当前 K 线。
        dataframe["funding_ann_pct"] = np.nan
        fr = self._funding_series(symbol)
        if fr is not None and len(fr[0]):
            ser = pd.Series(fr[1], index=pd.to_datetime(fr[0], unit="ms", utc=True))
            ser = ser[~ser.index.duplicated(keep="last")].sort_index()
            lag = ser.copy()
            lag.index = lag.index + pd.Timedelta(milliseconds=1)   # 下一根 K 线才可见
            aligned = lag.reindex(dataframe["date"], method="ffill")
            dataframe["funding_ann_pct"] = pd.to_numeric(aligned.to_numpy(), errors="coerce")
        # 缺失时回退到采集器最新快照(仅用于新上市合约的冷启动覆盖)
        dataframe["funding_ann_pct"] = dataframe["funding_ann_pct"].fillna(
            pd.to_numeric(dataframe["m3_ann"], errors="coerce"))
        dataframe["funding_ann_pct"] = pd.to_numeric(
            dataframe["funding_ann_pct"], errors="coerce").fillna(0.0)

        # 实时费率(ccxt premiumIndex, dry-run/live 可用): 作为门控叠加。
        # ⚠️ 必须缓存: 未缓存时 55 个交易对 × 每轮 1 次请求 ≈ 660 次/分钟, 会直接打爆
        # 币安 IP 限额(2400 weight/min, 且被同主机其它实例共享)并触发 429。
        # 资金费率在结算周期(通常 8h)内有效, 5 分钟 TTL 完全够用。
        self._refresh_live_funding(pair)
        return dataframe

    def _refresh_live_funding(self, pair: str, ttl_s: float = 300.0) -> None:
        """按 TTL 缓存实时资金费率, 显著降低交易所请求量."""
        now_s = time.time()
        cached = self._live_funding.get(pair)
        if cached and (now_s - float(cached.get("fetched_s", 0))) < ttl_s:
            return
        if self.dp is None or self.dp.runmode.value not in ("live", "dry_run"):
            self._live_funding[pair] = {"rate": 0.0, "next_ms": 0, "mark": 0.0,
                                        "fetched_s": now_s, "source": "none"}
            return
        try:
            fr = self.dp.funding_rate(pair) or {}
            if fr:
                self._live_funding[pair] = {
                    "rate": float(fr.get("fundingRate") or 0.0),
                    "next_ms": int(fr.get("fundingTimestamp") or 0),
                    "mark": float(fr.get("markPrice") or 0.0),
                    "fetched_s": now_s,
                    "source": "ccxt",
                }
                return
        except Exception as exc:  # noqa: BLE001
            log.debug("[M3] %s 实时费率获取失败(沿用缓存): %s", pair, exc)
        if not cached:
            self._live_funding[pair] = {"rate": 0.0, "next_ms": 0, "mark": 0.0,
                                        "fetched_s": now_s, "source": "error"}

    # ---------------------------------------------------------------- 入场
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """入场: 三周期共振 + 打分池 + 资金费率门控.

        实时资金费率(self.dp.funding_rate)优先; 取不到时回退到已结算历史年化。
        """
        cols = dataframe.columns
        pair = metadata.get("pair", "")
        live = self._live_funding.get(pair) or {}
        live_ann = (live.get("rate") or 0.0) * 3 * 365 if live else None
        try:
            data_ts = int(pd.Timestamp(dataframe["date"].iloc[-1]).timestamp() * 1000)
        except Exception:  # noqa: BLE001
            data_ts = _now_ms()
        has4h = "trend_dir_4h" in cols
        has1h = "phase_1h" in cols

        trend4 = dataframe.get("trend_dir_4h", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        phase1 = dataframe.get("phase_1h", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        adx1 = dataframe.get("adx_1h", pd.Series(20.0, index=dataframe.index)).fillna(20.0)
        rsi1 = dataframe.get("rsi_1h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)
        ext1 = dataframe.get("ext_pct_1h", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        adx5 = dataframe["adx"].fillna(0.0)
        rsi5 = dataframe["rsi"].fillna(50.0)
        rsi4 = dataframe.get("rsi_4h", pd.Series(50.0, index=dataframe.index)).fillna(50.0)
        # 4h 量能倍数(>1 = 有量能支撑). 注意列名是 volume_4h / vol_sma_4h
        _vol4 = pd.to_numeric(dataframe.get("volume_4h"), errors="coerce") if "volume_4h" in cols else None
        _vol4ma = pd.to_numeric(dataframe.get("vol_sma_4h"), errors="coerce") if "vol_sma_4h" in cols else None
        if _vol4 is not None and _vol4ma is not None:
            vol4_ok = (_vol4 / _vol4ma.replace(0, np.nan)).fillna(1.0)
        else:
            vol4_ok = pd.Series(1.0, index=dataframe.index)

        liquid = (dataframe["m3_quote_vol"] >= self.MIN_QUOTE_VOL) & (dataframe["m3_spread_bps"] <= 25)
        pool = dataframe["m3_in_pool"].astype(bool)
        # 脉冲票过滤: 稳定性因子 > 0(有过在榜历史)才考虑; 0 表示尚未统计到(冷启动放行)
        stick_ok = (dataframe["m3_stick"] >= self.MIN_STICK) | (dataframe["m3_stick"] == 0.0)

        # ---------------- 多头 ----------------
        long_ok = (
            pool & liquid & stick_ok
            & (trend4 >= 0.5 if has4h else pd.Series(True, index=dataframe.index))
            & (phase1 >= 0.5 if has1h else pd.Series(True, index=dataframe.index))
            & (adx1 >= 15)
            & (dataframe["m3_score"] >= self.MIN_SCORE_LONG)
            & (dataframe["funding_ann_pct"] <= self.FUNDING_MAX_LONG_ANN)
            & ((live_ann is None) or (live_ann <= self.FUNDING_MAX_LONG_ANN))
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["close"] > dataframe["ema_slow"])
            & (dataframe["macdhist"] > 0)
            & (rsi5 > 45) & (rsi5 < 78)
            & (rsi1 < 80)
            & (rsi4 < 78)                      # 4h 超买(>78)视为反转风险, 不追多
            & (ext1 < 12.0)                    # 不追已经远离均线的票
            # 动量甜区: 只在负期望区间之外做多(实测 10~20% 区间期望为负)
            & (dataframe["m3_change_24h"] >= self.MIN_CHANGE_24H)
            & (dataframe["mom_15m"] >= self.MIN_MOM_15M)
            & (dataframe["m3_oi_chg"] > -8.0)  # 资金没有大幅撤退
            & (vol4_ok >= 0.6)                 # 4h 趋势不能是「无量上涨」
            & (dataframe["volume"] > 0)
        )
        # 触发: 突破 / 回踩确认 / 动量重启 三者之一
        trig_long = (
            dataframe["breakout_up"]
            # 近 3 根内突破过 且 当根动量仍在 -> 视为有效突破延续
            | (dataframe["breakout_up_3"] & (dataframe["macdhist"] > 0))
            | ((dataframe["close"] > dataframe["ema_fast"]) & (dataframe["close"].shift(1) <= dataframe["ema_fast"].shift(1)))
            | ((dataframe["macdhist"] > 0) & (dataframe["macdhist"].shift(1) <= 0))
        )
        dataframe.loc[long_ok & trig_long, ["enter_long", "enter_tag"]] = (1, "m3_long")
        if live_ann is not None and live_ann > self.FUNDING_MAX_LONG_ANN:
            dataframe["enter_long"] = 0

        # ---------------- 空头 ----------------
        short_ok = (
            pool & liquid & stick_ok
            & (trend4 <= -0.5 if has4h else pd.Series(True, index=dataframe.index))
            & (phase1 <= -0.5 if has1h else pd.Series(True, index=dataframe.index))
            & (adx1 >= 15)
            & (dataframe["m3_score"] <= self.MIN_SCORE_SHORT)
            & (dataframe["funding_ann_pct"] >= self.FUNDING_MAX_SHORT_ANN)
            & ((live_ann is None) or (live_ann >= self.FUNDING_MAX_SHORT_ANN))
            & (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["close"] < dataframe["ema_slow"])
            & (dataframe["macdhist"] < 0)
            & (rsi5 < 55) & (rsi5 > 22)
            & (rsi1 > 20)
            & (rsi4 > 22)                      # 4h 超卖(<22)视为反弹风险, 不追空
            & (ext1 > -12.0)
            & (dataframe["m3_oi_chg"] > -8.0)
            & (vol4_ok >= 0.6)                 # 下跌同样要有量, 否则可能是流动性枯竭
            & (dataframe["volume"] > 0)
        )
        trig_short = (
            dataframe["breakout_dn"]
            | (dataframe["breakout_dn_3"] & (dataframe["macdhist"] < 0))
            | ((dataframe["close"] < dataframe["ema_fast"]) & (dataframe["close"].shift(1) >= dataframe["ema_fast"].shift(1)))
            | ((dataframe["macdhist"] < 0) & (dataframe["macdhist"].shift(1) >= 0))
        )
        dataframe.loc[short_ok & trig_short, ["enter_short", "enter_tag"]] = (1, "m3_short")

        # ---------------- 空头(独立路径): 多头极端拥挤时的均值回归做空 ----------------
        # 动机: 涨幅榜的典型亏钱模式是「追高 -> 被高费率+拥挤度拖死」。在**多头极端拥挤**
        # 时反手做空, 同时还能**收资金费率**(正费率 = 空头收钱), 是涨幅榜特有的机会。
        # 与上面的趋势做空互补: 这条不要求 4h 下跌趋势, 只要求「极端过热 + 动能刚转弱」。
        rev_ok = (
            pool & liquid
            & (dataframe["m3_score"] <= self.SHORT_REV_SCORE)      # 打分强烈看空
            & (dataframe["funding_ann_pct"] >= self.SHORT_REV_FUNDING_ANN)  # 多头付高额费率
            & (rsi4 >= self.SHORT_REV_RSI4)                        # 4h 超买
            & (rsi5 >= 60)                                         # 5m 仍强 -> 等它转弱
            & (dataframe["m3_oi_chg"] > -8.0)                      # 资金没撤退(OI 在, 拥挤真实)
            & (dataframe["close"] >= dataframe["ema_slow"] * 1.01) # 价格在均线上方(未破位)
            & (dataframe["volume"] > 0)
        )
        rev_trig = (
            ((dataframe["macdhist"] < 0) & (dataframe["macdhist"].shift(1) >= 0))  # 5m 动能转头
            | (dataframe["breakout_dn"])
            | ((dataframe["rsi"] < 50) & (dataframe["rsi"].shift(1) >= 50))        # RSI 下穿 50
        )
        dataframe.loc[rev_ok & rev_trig, ["enter_short", "enter_tag"]] = (1, "m3_short_rev")
        if live_ann is not None and live_ann < self.FUNDING_MAX_SHORT_ANN:
            dataframe["enter_short"] = 0

        # 熊市/极端贪婪抑制: 恐贪 > 85 时不做新多, < 15 时不做新空
        fng = dataframe["m3_fng"].fillna(50.0)
        dataframe.loc[fng > 85, "enter_long"] = 0
        dataframe.loc[fng < 15, "enter_short"] = 0

        # ---------- 信号率统计 + 入场漏斗诊断 ----------
        n_long = int(dataframe["enter_long"].fillna(0).astype(bool).sum())
        n_short = int(dataframe["enter_short"].fillna(0).astype(bool).sum())
        self._funnel_accum(pair, trend4, phase1, adx1, rsi5, rsi1, rsi4, ext1,
                           liquid, pool, vol4_ok, dataframe)
        if str(os.environ.get("M3_DEBUG_FUNNEL", "")) in ("1", "true") and (n_long or n_short):
            self._log_funnel(pair, dataframe, trend4, phase1, adx1, rsi5, rsi1, rsi4, ext1,
                             liquid, pool, vol4_ok)

        # 防抖冷却: 刚平仓的标的短期内不再开仓(减少插针反复收割)
        last_exit = self._last_exit.get(pair, 0)
        if last_exit:
            cooldown_ms = self.REENTRY_COOLDOWN_MIN * 60_000
            cutoff = data_ts - cooldown_ms
            ts_ms = pd.to_datetime(dataframe["date"], utc=True).astype("int64") // 10**6
            dataframe.loc[ts_ms >= cutoff, ["enter_long", "enter_short"]] = 0
        return dataframe

    # ---------------------------------------------------------------- 出场信号(趋势破坏)
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """趋势破坏离场.

        注意: 「极端拥挤反向做空」(m3_short_rev)路径**不**受此约束 —— 它的前提本就是
        在上涨趋势中逆势入场, 若等趋势反转才离场就失去意义; 该路径由 custom_exit 的
        费率回落 / RSI 回归 条件接管。
        """
        trend4 = dataframe.get("trend_dir_4h", pd.Series(0.0, index=dataframe.index)).fillna(0.0)
        phase1 = dataframe.get("phase_1h", pd.Series(0.0, index=dataframe.index)).fillna(0.0)

        # 趋势彻底反转或费率极端不利 -> 退出信号(实际离场仍受 custom_exit 逻辑约束)
        exit_long = (phase1 <= -0.5) | (trend4 <= -0.5)
        exit_short = (phase1 >= 0.5) | (trend4 >= 0.5)
        dataframe.loc[exit_long, ["exit_long", "exit_tag"]] = (1, "trend_break")
        dataframe.loc[exit_short, ["exit_short", "exit_tag"]] = (1, "trend_break")
        return dataframe

    def _funnel_accum(self, pair: str, trend4, phase1, adx1, rsi5, rsi1, rsi4, ext1,
                      liquid, pool, vol4_ok, dataframe: DataFrame) -> None:
        """累积各道入场门槛的通过次数, 每 200 次评估汇总打印一次.

        诊断目的: 当信号过少时, 用数据定位到底是哪一道门槛在淘汰绝大多数候选,
        而不是凭感觉放宽条件。
        """
        i = len(dataframe) - 1
        row = dataframe.iloc[i]
        st = self._sig_stat
        st["pairs"] = st.get("pairs", 0) + 1
        st["long"] = st.get("long", 0) + int(bool(row.get("enter_long")))
        st["short"] = st.get("short", 0) + int(bool(row.get("enter_short")))

        gates = {
            "pool": bool(pool.iloc[i]),
            "liq": bool(liquid.iloc[i]),
            "trend_same": abs(float(trend4.iloc[i])) >= 0.5 and abs(float(phase1.iloc[i])) >= 0.5,
            "adx": float(adx1.iloc[i]) >= 15,
            "score12": abs(float(row.get("m3_score", 0) or 0)) >= 12,
            "funding_ok": abs(float(row.get("funding_ann_pct", 0) or 0)) <= 0.45,
            "ma_align": bool((row.get("ema_fast", 0) > row.get("ema_slow", 0))
                             or (row.get("ema_fast", 0) < row.get("ema_slow", 0))),
            "macd_dir": bool((row.get("macdhist", 0) > 0) or (row.get("macdhist", 0) < 0)),
            "rsi5_range": 22 < float(rsi5.iloc[i]) < 78,
            "rsi4_extreme": 22 < float(rsi4.iloc[i]) < 78,
            "ext": abs(float(ext1.iloc[i])) < 12.0,
            "oi": float(row.get("m3_oi_chg", 0) or 0) > -8.0,
            "vol4": float(vol4_ok.iloc[i]) >= 0.6,
            "trigger": bool(row.get("breakout_up") or row.get("breakout_dn_3")),
        }
        acc = st.setdefault("gates", {})
        for k, ok in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(ok))
        # 记录候选池内样本的通过情况(未在池内的合约不参与门槛统计)
        if gates["pool"]:
            st["in_pool"] = st.get("in_pool", 0) + 1
            accp = st.setdefault("gates_pool", {})
            for k, ok in gates.items():
                if k != "pool":
                    accp[k] = accp.get(k, 0) + int(bool(ok))

        if st["pairs"] % 50 == 0:
            n = st["pairs"]; np_ = max(st.get("in_pool", 0), 1)
            log.info("[M3] 信号率: 评估 %d 次(池内 %d), 多头 %d, 空头 %d",
                     n, st.get("in_pool", 0), st["long"], st["short"])
            accp = st.get("gates_pool", {})
            log.info("[M3] 池内各门槛通过率(%%): %s", "  ".join(
                f"{k}={accp.get(k,0)/np_*100:.0f}" for k in
                ("liq", "trend_same", "adx", "score12", "funding_ok", "ma_align",
                 "macd_dir", "rsi5_range", "rsi4_extreme", "ext", "oi", "vol4", "trigger")))

    def _log_funnel(self, pair: str, dataframe: DataFrame, trend4, phase1, adx1, rsi5, rsi1,
                    rsi4, ext1, liquid, pool, vol4_ok) -> None:
        """打印最后一根 K 线在各道门槛上的通过情况, 用于定位信号数量骤减的原因."""
        i = len(dataframe) - 1
        row = dataframe.iloc[i]
        checks = [
            ("在候选池内", bool(pool.iloc[i])),
            ("流动性(额/价差)", bool(liquid.iloc[i])),
            ("4h主趋势同向", abs(float(trend4.iloc[i])) >= 0.5),
            ("1h相位同向", abs(float(phase1.iloc[i])) >= 0.5),
            ("1h ADX>=15", float(adx1.iloc[i]) >= 15),
            ("M3得分达标", abs(float(row.get("m3_score", 0) or 0)) >= 12),
            ("费率可接受", True),
            ("5m均线排列", True),
            ("5m RSI 区间", 45 < float(rsi5.iloc[i]) < 78),
            ("4h RSI 未极端", 22 < float(rsi4.iloc[i]) < 78),
            ("1h 延伸度<12%", abs(float(ext1.iloc[i])) < 12.0),
            ("OI 未大幅撤退", float(row.get("m3_oi_chg", 0) or 0) > -8.0),
            ("4h 量能>=0.6", float(vol4_ok.iloc[i]) >= 0.6),
            ("触发条件", bool(row.get("breakout_up") or row.get("breakout_dn"))),
        ]
        passed = sum(1 for _, ok in checks if ok)
        log.info("[M3][漏斗] %-16s 通过 %d/%d | 未通过: %s | score=%.1f 费率年化=%.1f%% "
                 "4h趋势=%.0f 1h相位=%.0f", pair, passed, len(checks),
                 ",".join(n for n, ok in checks if not ok) or "无",
                 float(row.get("m3_score", 0) or 0),
                 float(row.get("funding_ann_pct", 0) or 0) * 100,
                 float(trend4.iloc[i]), float(phase1.iloc[i]))

    # ================================================================
    #  动态仓位与杠杆
    # ================================================================
    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs: Any) -> float:
        """自适应杠杆: 强趋势高波动 -> 降杠杆; 再用「仓位不会顶到上限」的安全界夹住."""
        lev = self.leverage_value
        atr_frac = 0.03
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and len(df):
                row = df.iloc[-1]
                atr_pct = float(row.get("atr_pct", 0) or 0)
                atr_frac = max(atr_pct / 100.0, 0.005)
                adx = float(row.get("adx", 0) or 0)
                if atr_pct > 6.0:
                    lev -= 1.5
                elif atr_pct > 3.5:
                    lev -= 0.75
                if adx > 35:
                    lev += 0.5
                score = abs(float(row.get("m3_score", 0) or 0))
                if score > 45:
                    lev += 0.5
        except Exception:  # noqa: BLE001
            pass
        # 安全界: 保证「止损距离 × 杠杆」不会把仓位顶到上限(stops_core.leverage_for_risk)
        stop_dist = entry_stop_distance(atr_frac, self.STOP_ATR_MULT)
        safe = leverage_for_risk(stop_dist, max_leverage=max_leverage,
                                 base_leverage=min(lev, self.max_leverage),
                                 target_ratio=self.TARGET_STAKE_RATIO)
        return float(max(1.0, min(lev, self.max_leverage, max_leverage, safe)))

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float],
                            max_stake: float, leverage: float, entry_tag: Optional[str],
                            side: str, **kwargs: Any) -> float:
        """仓位规模: 以「止损触发时的权益回撤」为约束反解保证金.

        ⚠️ 关键单位关系(2026-09-11 曾漏乘杠杆, 使实际风险放大 4 倍):
            实际名义敞口 = stake × leverage
            止损损失     = 名义敞口 × 止损价格距离 = stake × leverage × d
            因此 stake/权益 = risk_budget / (d × leverage)
        完整决策(风险硬上限 + 最小仓位回退)在 stops_core.plan_position(), 有单测覆盖:
            freqtrade/tests/unit/test_stops_core.py
        """
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            if row is None:
                return proposed_stake
            atr_pct = float(row.get("atr_pct", 3.0) or 3.0)
            score = abs(float(row.get("m3_score", 0) or 0))
            ann = float(row.get("funding_ann_pct", 0) or 0)

            # 与持仓期 custom_stoploss 使用完全相同的函数与边界(口径统一)
            stop_dist = entry_stop_distance(atr_pct / 100.0, self.STOP_ATR_MULT)
            wallet = self.wallets.get_total_stake_amount() if self.wallets else 0.0
            if wallet <= 0:
                return proposed_stake

            # 榜单稳定性调整: 长期霸榜 = 更值得多下注; 脉冲票 = 减仓
            stick = float(row.get("m3_stick", 0.0) or 0.0)
            conf = 1.0 + min(score, 60.0) / 150.0
            if stick >= 0.6:
                conf *= 1.25
            elif stick <= 0.2:
                conf *= 0.7
            plan = plan_position(
                equity=wallet, risk_budget=self.RISK_BUDGET, price_stop_distance=stop_dist,
                leverage=leverage, min_ratio=self.MIN_STAKE_RATIO,
                max_ratio=self.MAX_STAKE_RATIO, risk_ceiling=self.RISK_CEILING,
                confidence_mult=conf, ann_funding=ann,
            )
            if not plan.ok:
                log.warning("[M3] %s 放弃信号: %s (止损距离%.2f%% 杠杆%.1f)",
                            pair, plan.reason, stop_dist * 100, leverage)
                return 0.0

            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            if min_stake and stake < min_stake:
                stake = min(min_stake, max_stake * 0.9 if max_stake else min_stake)
            # 记录设计风险(供平仓时对照)
            self._diag[pair] = {
                "stop_dist": stop_dist, "leverage": float(leverage), "stake": float(stake),
                "design_risk": float(plan.risk_pct), "atr_pct": atr_pct, "score": score,
                "ann": ann, "planned_stake": float(plan.stake),
            }
            log.info("[M3] %s 仓位: 止损距离%.2f%% 杠杆%.1f score%.0f 费率%.1f%% -> "
                     "保证金%.2f USDT (名义%.2f = 权益%.1f%%, 止损触发权益回撤%.2f%%, 上限%.2f%%) [%s]",
                     pair, stop_dist * 100, leverage, score, ann * 100, stake, stake * leverage,
                     stake * leverage / max(wallet, 1e-9) * 100,
                     plan.risk_pct * 100, self.RISK_CEILING * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[M3] 仓位计算异常 %s: %s", pair, exc)
            return proposed_stake

    # ================================================================
    #  止损 / 跟踪止盈
    # ================================================================
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs: Any) -> Optional[float]:
        """ATR 跟踪止损 + 盈利保护(持续持有 + 持续收割的核心).

        ⚠️ 单位约定(极易出错, 此处只用价格口径):
           - freqtrade 合约模式下 custom_stoploss 的返回值是「相对当前价的**风险比例**」,
             即 价格距离 = |返回值| / leverage;
           - current_profit 是「**含杠杆**的权益收益率」, 价格收益率 = current_profit / leverage;
           - 本函数内部一律使用 **价格距离 d**(正数, 相对当前价), 最后统一用
             stoploss_from_absolute() 换算成返回值。
           - 早期版本把「权益收益率」与「价格距离」混用(例如
             lock = current_profit/lev - MIN_LOCK_PROFIT), 在 4x 杠杆下会算出
             0.1% 级别的价格距离, 等于把止损贴到现价上, 会被正常波动立刻扫出。

        策略意图:
           ① 初始阶段: 用 2.6×ATR(5m) 的宽止损给趋势留出发育空间(避免被插针扫掉);
           ② 浮盈超过阈值后: 切换为 2.2×ATR 跟踪, 且「止损距离不高于盈利保护因子×价格获利」
              —— 即始终至少锁定 60% 的已实现价格涨幅;
           ③ 大浮盈阶段继续收紧, 最终锁定大部分利润。
        """
        lev = float(trade.leverage or 1.0)
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            atr_frac = float(row.get("atr_pct", 3.0) or 3.0) / 100.0 if row is not None else 0.03
        except Exception:  # noqa: BLE001
            atr_frac = 0.03

        is_short = bool(trade.is_short)
        # 峰值浮盈: 用持仓期最高价反算, 作为盈利保护的基准(保证止损单调收紧)
        peak_profit = current_profit
        try:
            if trade.max_rate:
                if is_short:
                    peak_profit = (trade.open_rate - trade.max_rate) / trade.open_rate * lev
                else:
                    peak_profit = (trade.max_rate - trade.open_rate) / trade.open_rate * lev
        except Exception:  # noqa: BLE001
            peak_profit = current_profit
        # 数学集中在 stops_core(纯函数 + 单测), 此处只做「取数据 -> 换算 -> 返回」
        d, stage = stop_price_distance(current_profit, lev, atr_frac, self._stop_params(),
                                       peak_profit=peak_profit)
        stop_price = stop_rate(current_rate, d, is_short)
        val = freqtrade_stoploss_value(current_rate, stop_price, lev, is_short)

        self._sl_calls = getattr(self, "_sl_calls", 0) + 1
        if self._sl_calls % 100 == 1:
            log.info("[M3] 止损计算 %s [%s]: 权益收益%.2f%% 价格收益%.2f%% 价格距离%.2f%% "
                     "止损价%.6g 现价%.6g 杠杆%.1f -> 返回%.4f",
                     pair, stage, current_profit * 100, current_profit / max(lev, 1.0) * 100,
                     d * 100, stop_price, current_rate, lev, val)
        return float(val) if val > 0 else None

    def _stop_params(self) -> StopParams:
        """把策略参数打包给 stops_core(保持策略内的数值即唯一配置源)."""
        return StopParams(
            stop_atr_mult=self.STOP_ATR_MULT,
            trail_atr_mult=self.TRAIL_ATR_MULT,
            trail_start_profit=self.TRAIL_START_PROFIT,
            hard_stop=self.HARD_STOP,
            profit_protect=self.PROFIT_PROTECT,
            trail_tight_1_mult=self.TRAIL_TIGHT_1,
            trail_tight_2_mult=self.TRAIL_TIGHT_2,
            trail_tight_1_profit_at=self.TRAIL_START_PROFIT * 1.6,
            trail_tight_2_profit_at=self.TRAIL_START_PROFIT * 3.0,
        )

    # ================================================================
    #  自定义离场: 资金费率失衡 / 动量衰竭 / 超时
    # ================================================================
    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        """记录平仓时间以启用冷却窗口; 永远允许离场."""
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[M3][平仓] %s %s 原因=%s | 设计风险%.2f%% 实际%.2f%% (差%+.2f%%) | "
                     "止损距离%.2f%% 杠杆%.1f 保证金%.2f score%.0f 费率%.1f%% | "
                     "持有%.0f分钟 冷却至 %s",
                     pair, "空" if trade.is_short else "多", exit_reason,
                     d["design_risk"] * 100, ratio * 100, (ratio - d["design_risk"]) * 100,
                     d["stop_dist"] * 100, d["leverage"], d["stake"], d["score"], d["ann"] * 100,
                     (current_time - trade.open_date_utc).total_seconds() / 60.0,
                     _ms_to_utc(self._last_exit[pair] + self.REENTRY_COOLDOWN_MIN * 60_000))
        else:
            log.info("[M3][平仓] %s %s 原因=%s 收益率%.2f%% (无开仓埋点)",
                     pair, "空" if trade.is_short else "多", exit_reason, ratio * 100)
        return True

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None

        is_short = trade.is_short
        ann = float(row.get("funding_ann_pct", 0) or 0)
        rsi4 = float(row.get("rsi_4h", 50) or 50)
        phase1 = float(row.get("phase_1h", 0) or 0)

        # ①' 「极端拥挤反向做空」专属: 过热消退即收割(不依赖趋势反转)
        if (trade.enter_tag or "") == "m3_short_rev":
            if ann <= 0.10:
                return "rev_funding_cooled"      # 费率回落 -> 拥挤已释放, 收工
            if rsi4 <= 58:
                return "rev_rsi_normalized"      # 超买修复完成
            if phase1 <= -0.5:
                return "rev_trend_turned"        # 趋势真的转空, 已获利
            if not is_short:
                return "unexpected_direction"
        rsi5 = float(row.get("rsi", 50) or 50)
        macdh = float(row.get("macdhist", 0) or 0)
        rsi1 = float(row.get("rsi_1h", 50) or 50)
        hold_h = (current_time - trade.open_date_utc).total_seconds() / 3600.0

        # ① 资金费率严重不利: 持有成本吃掉预期收益
        if not is_short and ann > self.FUNDING_EXIT_ANN:
            return "funding_exit_long"
        if is_short and ann < -(self.FUNDING_EXIT_ANN):
            return "funding_exit_short"

        # ② 动能衰竭且已有浮盈 -> 收割
        if current_profit > 0.03:
            if not is_short and rsi5 > 82 and macdh < 0:
                return "momentum_fade_long"
            if is_short and rsi5 < 18 and macdh > 0:
                return "momentum_fade_short"
        if not is_short and current_profit > 0.05 and rsi1 > 88:
            return "overbought_1h"
        if is_short and current_profit > 0.05 and rsi1 < 12:
            return "oversold_1h"

        # ③ 长期横盘无进展 -> 释放资金
        if hold_h > 72 and abs(current_profit) < 0.004:
            return "stale_position"
        if hold_h > self.MAX_HOLD_HOURS:
            return "max_hold_time"
        return None

    # ================================================================
    #  分批收割(持续收割的核心)
    # ================================================================
    def adjust_trade_position(self, trade: Trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs: Any) -> Optional[float]:
        """浮盈达标后分批减仓(负值=减仓). 只减不加, 保证风险单调收敛.

        持续持有 = 主仓不动 + 用部分利润兑现, 避免"全有全无"的离场。
        """
        if trade.has_open_orders:
            return None
        n_exits = int(trade.nr_of_successful_exits or 0)
        if n_exits >= 2:
            return None
        # 阶梯收割: 第一次在 +10% 减 35%, 第二次在 +30% 再减 35%
        if n_exits == 0 and current_profit >= self.TAKE_PARTIAL_AT:
            amount = trade.stake_amount * self.PARTIAL_RATIO
        elif n_exits == 1 and current_profit >= self.TAKE_PARTIAL_AT * 3:
            amount = trade.stake_amount * self.PARTIAL_RATIO
        else:
            return None
        if min_stake and amount < min_stake:
            return None
        log.info("[M3] %s 分批收割第%d次: 浮盈 %.2f%%, 减仓 stake=%.2f (当前价 %.6g)",
                 trade.pair, n_exits + 1, current_profit * 100, amount, current_rate)
        return -float(amount)

    # ================================================================
    #  外部数据读取
    # ================================================================
    def _refresh_watchlist(self) -> None:
        now = _now_ms()
        if now - self._wl_ms < 20_000 and self._wl:
            return
        try:
            raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            self._wl = {c["symbol"]: c for c in raw.get("candidates", []) if c.get("symbol")}
            self._macro = {k: float(v) for k, v in (raw.get("macro") or {}).items()}
            self._wl_ms = now
        except Exception as exc:  # noqa: BLE001
            if now - self._wl_ms > 300_000:
                log.warning("[M3] watchlist 读取失败: %s", exc)
                self._wl_ms = now

    def _funding_series(self, symbol: str) -> Optional[tuple[list[int], list[float]]]:
        """读取结算资金费率历史(UTC ms + 费率). 严格使用已结算数据."""
        try:
            if self._db_conn is None:
                if not MARKET_DB.exists():
                    return None
                self._db_conn = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True,
                                                timeout=10, check_same_thread=False)
            cur = self._db_conn.execute(
                "SELECT ts_ms, funding_rate FROM funding_hist WHERE symbol=? ORDER BY ts_ms",
                (symbol,))
            rows = cur.fetchall()
            if not rows:
                return None
            return [int(r[0]) for r in rows], [float(r[1]) for r in rows]
        except Exception:  # noqa: BLE001
            return None
