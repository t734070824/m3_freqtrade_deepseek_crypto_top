"""M3-DSH 多源数据采集器 (容器 m3dsc-market-collector).

采集矩阵
--------
Binance USD-M (交易所场内, 全部无需 API Key):
  ticker  60s : /fapi/v1/ticker/24hr 全市场  -> 涨幅榜基础 + 成交额
  mark    60s : /fapi/v1/premiumIndex 全市场 -> 标记价 + **资金费率** + 下次结算时间
  oi     180s : /fapi/v1/openInterest + /futures/data/openInterestHist  -> 持仓量及其变化
  ratio  300s : 大户账户/大户持仓/全局账户多空比 + taker 主动买卖比
  book   120s : /fapi/v1/ticker/bookTicker -> 盘口价差(流动性)
  basis  300s : /futures/data/basis -> 期现基差
场外 (macro):
  fng    900s : 恐贪指数 (alternative.me, 免费)
  news   600s : 加密货币新闻 RSS (免费)

产出
----
* data/m3dsc_market.db        : 全量时序 (SQLite WAL)
* data/live/watchlist.json    : 选币打分引擎输出的候选池 (供 freqtrade 策略读取)
* logs/collector.log
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

if __package__ in (None, ""):  # 支持 python dshc/collector.py 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dshc.binance import (BinanceFutures, NON_TRADABLE_BASES, parse_premium_row,
                              parse_ticker_row, _f)
    from dshc.config import SETTINGS
    from dshc.db import heartbeat, init_db, prune, upsert_many
    from dshc.screener import build_candidates, latest_macro
    from dshc.timeutil import fmt, fmt_both, fmt_cn, now_cst, parse_binance_ms, utc_ms
else:
    from .binance import (BinanceFutures, NON_TRADABLE_BASES, parse_premium_row,
                          parse_ticker_row, _f)
    from .config import SETTINGS
    from .db import heartbeat, init_db, prune, upsert_many
    from .screener import build_candidates, latest_macro
    from .timeutil import fmt, fmt_both, fmt_cn, now_cst, parse_binance_ms, utc_ms

log = logging.getLogger("dshc.collector")


class StreamHandler(logging.StreamHandler):
    """管到文件时 Python 会切到块缓冲, 这里强制每行 flush, 便于 docker logs 实时排障."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        super().emit(record)
        self.flush()

# 采集规模(按成交额取前 N), 在 IP 限额内
N_MARK = 200
N_OI = 90
N_RATIO = 60
N_BOOK = 90
N_BASIS = 40

RETAIN_MS = {
    "ticker_snap": 4 * 24 * 3600_000,
    "perp_mark": 4 * 24 * 3600_000,
    "oi_now": 4 * 24 * 3600_000,
    "oi_hist": 4 * 24 * 3600_000,
    "ls_ratio": 7 * 24 * 3600_000,
    "funding_hist": 420 * 24 * 3600_000,   # 保留 14 个月: 费率极值判断需要长历史
    "book_snap": 12 * 3600_000,
    "basis_snap": 4 * 24 * 3600_000,
    "rank_snap": 30 * 24 * 3600_000,
    "news": 14 * 24 * 3600_000,
}

MACRO_FNG_URL = "https://api.alternative.me/fng/"


# =====================================================================
#  采集器状态
# =====================================================================
@dataclass
class WorkerStat:
    name: str
    interval: int
    runs: int = 0
    errors: int = 0
    rows: int = 0
    last_ok_ms: int = 0
    last_error: str = ""
    last_dur_ms: int = 0


class MarketCollector:
    def __init__(self, *, proxy: str = "", db_path: Path | None = None,
                 live_dir: Path | None = None, parallel: int = 6,
                 enable_fng: bool = True, enable_news: bool = True) -> None:
        self.s = SETTINGS
        self.db_path = db_path or self.s.db_path
        self.live_dir = live_dir or (self.s.data_dir / "live")
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self.api = BinanceFutures(proxy=proxy or self.s.proxy, rps=3.0)
        self.parallel = max(1, parallel)
        self.enable_fng = enable_fng
        self.enable_news = enable_news

        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._ping_lock = threading.Lock()
        self._next_slot = 0.0
        self._stop = threading.Event()
        self._stats: dict[str, WorkerStat] = {}
        self._symbols_lock = threading.Lock()
        self.tradable: list[str] = []
        self.by_quote_vol: list[str] = []
        self._meta_loaded_ms = 0
        self._known_symbols: set[str] = set()
        self._funding_filled: set[str] = set()
        self._funding_cursor: int = 0
        self._started_ms = utc_ms()

    # ------------------------------------------------------------ 生命周期
    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = init_db(self.db_path)
            self.conn.execute("PRAGMA busy_timeout=60000")
        return self.conn

    def stop(self, *_a: Any) -> None:
        log.info("收到停止信号, 正在优雅退出 ...")
        self._stop.set()

    def install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except ValueError:  # 非主线程
                pass

    # ------------------------------------------------------------ 元数据
    def refresh_meta(self, force: bool = False) -> None:
        if not force and utc_ms() - self._meta_loaded_ms < 6 * 3600_000 and self.tradable:
            return
        rows = self.api.perp_symbols()
        now = utc_ms()
        payload = []
        for s in rows:
            f = s.get("filters") or []
            filt = {x.get("filterType"): x for x in f if isinstance(x, dict)}
            tick = _f((filt.get("PRICE_FILTER") or {}).get("tickSize"))
            minq = _f((filt.get("LOT_SIZE") or {}).get("minQty"))
            minnot = _f((filt.get("MIN_NOTIONAL") or {}).get("notional"))
            payload.append((
                s["symbol"], s.get("baseAsset"), s.get("quoteAsset"),
                s.get("contractType"), s.get("status"),
                int(s.get("onboardDate") or 0), int(s.get("deliveryDate") or 0),
                s.get("pricePrecision"), s.get("quantityPrecision"),
                tick, minq, minnot, None,
                int(s.get("fundingIntervalHours") or 8), now,
            ))
        with self._lock:
            upsert_many(self.connect(), "perp_meta",
                        ["symbol", "base", "quote", "contract_type", "status", "onboard_ms",
                         "delivery_ms", "price_precision", "qty_precision", "tick_size",
                         "min_qty", "min_notional", "last_funding_rate",
                         "funding_interval_hours", "updated_ms"], payload)
            self.connect().commit()
        with self._symbols_lock:
            self.tradable = [s["symbol"] for s in rows]
            self._known_symbols = set(self.tradable)
        self._meta_loaded_ms = now
        log.info("合约元数据已刷新: %d 个可交易 USDT 永续合约 (数据时间 %s)",
                 len(self.tradable), fmt_both(now))

    # ------------------------------------------------------------ 工具
    def top_symbols(self, n: int) -> list[str]:
        with self._symbols_lock:
            have = set(self.tradable)
            seq = [s for s in self.by_quote_vol if s in have]
        return seq[:n]

    def _pmap(self, fn: Callable[[str], Any], symbols: Sequence[str],
              *, desc: str = "", limit_rps: float = 3.0) -> list[Any]:
        """并发调用单 symbol 接口, 返回成功结果列表(失败仅记录).

        注意: 币安「每 IP 2400 weight/min」是与同主机其它实例共享的, 因此本采集器
        主动把并发与速率压到保守水平, 并让 /futures/data/* 单独限速。
        """
        import time as _t
        out: list[Any] = []
        self._throttle(limit_rps)
        errs = 0
        with ThreadPoolExecutor(max_workers=self.parallel) as ex:
            futs = {ex.submit(fn, s): s for s in symbols}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    if r:
                        out.append(r)
                except Exception as exc:  # noqa: BLE001
                    errs += 1
                    if errs <= 3:
                        log.warning("%s 采集失败 %s: %s", desc, futs[fut], exc)
        if errs:
            log.info("%s: 成功 %d / 失败 %d", desc or "并发采集", len(out), errs)
        return out

    def _throttle(self, rps: float) -> None:
        """跨线程共享的最小间隔限速(保护同 IP 上的其它实例)."""
        with self._ping_lock:
            now = time.monotonic()
            gap = 1.0 / max(rps, 0.1)
            wait = self._next_slot - now
            if wait > 0:
                time.sleep(min(wait, 5.0))
                now = time.monotonic()
            self._next_slot = max(now, self._next_slot) + gap

    def _mark_stat(self, st: WorkerStat, rows: int, error: str = "") -> None:
        now = utc_ms()
        st.runs += 1
        st.rows = rows
        st.last_dur_ms = 0
        if error:
            st.errors += 1
            st.last_error = error[:400]
        else:
            st.last_ok_ms = now
            st.last_error = ""
        with self._lock:
            try:
                heartbeat(self.connect(), st.name, rows=rows, error=error or None, now_ms=now)
                self.connect().commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("心跳写入失败: %s", exc)

    # ================================================================ 采集任务
    def job_ticker(self) -> int:
        """全市场 24h 行情 -> 涨幅榜基础 (一次请求拿全市场, weight=40)."""
        self.refresh_meta()
        now = int(time.time() // 60 * 60_000)
        data = self.api.ticker_24hr()
        rows = [parse_ticker_row(t, now) for t in data if t.get("symbol") in self._known_symbols]
        with self._lock:
            upsert_many(self.connect(), "ticker_snap",
                        ["ts_ms", "symbol", "price", "price_change_pct", "quote_vol",
                         "base_vol", "trade_count", "high_24h", "low_24h", "open_24h",
                         "weighted_avg"], rows)
            self.connect().commit()

        # 排行榜(按成交额)
        ranked = sorted(rows, key=lambda r: (r[4] or 0.0), reverse=True)
        with self._symbols_lock:
            self.by_quote_vol = [r[1] for r in ranked]

        # 涨幅榜 -> watchlist.json + univ 表 (供 freqtrade 策略/Pairlist 使用)
        self.write_watchlist()
        return len(rows)

    def write_watchlist(self) -> None:
        try:
            cands = build_candidates(
                self.connect(), top_n=self.s.top_n,
                min_quote_vol=self.s.min_quote_vol_usdt,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("打分引擎失败: %s", exc)
            return
        now = utc_ms()
        macro = latest_macro(self.connect())
        # ⚠️ 时区陷阱: 本进程所在容器的 TZ=Asia/Shanghai, 若再手动 +8 小时会「双重偏移」。
        # 统一改用 timeutil(按 UTC 时间戳 + 显式时区对象换算), 不依赖进程本地时区。
        payload = {
            "generated_ms": now,
            "generated_utc": fmt(now),
            "generated_cst": fmt_cn(now),
            "macro": macro,
            "n_tradable": len(self.tradable),
            "candidates": [c.to_payload() for c in cands],
        }
        tmp = self.live_dir / "watchlist.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.live_dir / "watchlist.json")

        # ---- 榜单/打分快照落库 (5 分钟桶) ----
        # ⚠️ 关键: rank 必须是「**全市场** 24h 涨幅名次」, 而不是候选池内的序号。
        # 早期实现误用候选池序号, 导致所有候选的「在榜稳定性」都变成 100%, 因子完全失效。
        try:
            ts5 = now - (now % 300_000)
            # 全市场涨幅榜(含流动性过滤), 用于真实名次
            # ⚠️ 必须用「分钟粒度」的 now 查 ticker_snap —— ts5 是 5 分钟桶, ticker_snap 里没有
            # 这个时间戳(每行只在整分钟写入)。早期写成 ts5 导致所有候选名次都退化成 9999。
            univ = list(self.connect().execute(
                "SELECT symbol, price_change_pct q, quote_vol v FROM ticker_snap WHERE ts_ms=?",
                (now,)))
            if not univ:                       # 兜底: 取最新一分钟
                mx = self.connect().execute("SELECT MAX(ts_ms) FROM ticker_snap").fetchone()[0]
                univ = list(self.connect().execute(
                    "SELECT symbol, price_change_pct q, quote_vol v FROM ticker_snap "
                    "WHERE ts_ms=?", (mx,)))
            ranked = sorted((r for r in univ if (r["v"] or 0) >= self.s.min_quote_vol_usdt),
                            key=lambda r: r["q"] or -999.0, reverse=True)
            rank_map = {r["symbol"]: i + 1 for i, r in enumerate(ranked)}

            rows = []
            for c in cands:
                rows.append((ts5, c.symbol, rank_map.get(c.symbol, 9999), c.score, c.change_24h,
                             c.funding_ann, c.oi_chg_1h, c.ls_ratio, c.taker_ratio, c.spread_bps,
                             ",".join(c.tags)))
            # 额外记录全市场涨幅榜 TOP50(即使不在候选池), 供名次稳定性分析
            have = {c.symbol for c in cands}
            by_sym = {r["symbol"]: r for r in univ}
            for sym, rk in list(rank_map.items())[:50]:
                if sym in have:
                    continue
                r = by_sym.get(sym)
                rows.append((ts5, sym, rk, 0.0, (r["q"] if r else 0.0) or 0.0, 0.0, 0.0,
                             0.0, 1.0, 0.0, "UNIV"))
            if rows:
                with self._lock:
                    upsert_many(self.connect(), "rank_snap",
                                ["ts_ms", "symbol", "rank", "score", "change_24h", "funding_ann",
                                 "oi_chg_1h", "ls_ratio", "taker_ratio", "spread_bps", "tags"],
                                rows)
                    self.connect().commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("rank_snap 落库失败: %s", exc)

        # 涨幅榜前 N 名符号文件(供人工核对)
        gainers = sorted(
            [(c.symbol, c.change_24h) for c in cands if c.quote_vol >= self.s.min_quote_vol_usdt],
            key=lambda x: x[1], reverse=True)[:120]
        (self.live_dir / "gainers.txt").write_text(
            "\n".join(s for s, _ in gainers) + "\n", encoding="utf-8")

        # ---- freqtrade RemotePairList 契约文件 ----
        # freqtrade 2026.x 的 pairlists[].method 是枚举白名单校验的, 用户无法注册
        # 自定义 IPairList; 官方留给用户的扩展点就是 RemotePairList(支持 file:///)。
        # 交付格式: {"pairs": ["BTC/USDT:USDT", ...], "refresh_period": N}
        top = cands[:self.s.top_n]
        pairs = [f"{c.symbol[:-4]}/USDT:USDT" for c in top if c.symbol.endswith("USDT")]
        if not pairs:  # 冷启动兜底, 避免 freqtrade 因空池启动失败
            pairs = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        for c in top:
            if c.symbol.endswith("USDT"):
                continue
        pairlist_doc = {
            "pairs": pairs,
            "refresh_period": 60,
            "generated_ms": now,
            "generated_cst": payload["generated_cst"],
            "generated_utc": payload["generated_utc"],
        }
        tmp2 = self.live_dir / "pairs.json.tmp"
        tmp2.write_text(json.dumps(pairlist_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp2.replace(self.live_dir / "pairs.json")

    def job_mark(self) -> int:
        """标记价 + 资金费率(全市场一次请求) -> 年化费率与下次结算时间."""
        self.refresh_meta()
        now = int(time.time() // 60 * 60_000)
        data = self.api.premium_index()
        rows = [parse_premium_row(p, now) for p in data if p.get("symbol") in self._known_symbols]
        with self._lock:
            upsert_many(self.connect(), "perp_mark",
                        ["ts_ms", "symbol", "mark_price", "index_price", "last_funding_rate",
                         "next_funding_time_ms", "interest_rate", "funding_interval_hours",
                         "pred_funding_rate"], rows)
            self.connect().commit()
        return len(rows)

    def job_oi(self) -> int:
        """持仓量: 最新值(全部候选) + 交易所 5m 历史(前 N)."""
        syms = self.top_symbols(N_OI)
        if not syms:
            return 0
        now, now_min = utc_ms(), int(time.time() // 60 * 60_000)

        live = self._pmap(lambda s: (s, self.api.open_interest(s)), syms, desc="openInterest")
        # openInterest 接口只给币本位数量, 名义价值 = 持仓量 * 标记价(取自 perp_mark 最新快照)
        marks = {r["symbol"]: r["m"] for r in self.connect().execute(
            "SELECT m.symbol, m.mark_price m FROM perp_mark m "
            "JOIN (SELECT symbol, MAX(ts_ms) mx FROM perp_mark GROUP BY symbol) x "
            "ON m.symbol=x.symbol AND m.ts_ms=x.mx")}
        rows_now = []
        for s, r in live:
            oi = _f(r.get("openInterest"))
            mark = marks.get(s)
            rows_now.append((now_min, s, oi, (oi * mark) if (oi and mark) else None))

        hist: list[tuple] = []
        few = syms[:30]
        for item in self._pmap(lambda s: (s, self.api.open_interest_hist(s, "5m", 12)), few,
                               desc="oiHist"):
            sym, data = item if isinstance(item, tuple) and len(item) == 2 else (None, None)
            if not sym or not isinstance(data, list):
                continue
            for d in data:
                if not isinstance(d, dict) or "timestamp" not in d:
                    continue
                hist.append((parse_binance_ms(d["timestamp"]), sym,
                             _f(d.get("sumOpenInterest")), _f(d.get("sumOpenInterestValue")), "5m"))

        with self._lock:
            upsert_many(self.connect(), "oi_now", ["ts_ms", "symbol", "oi", "oi_value"], rows_now)
            if hist:
                upsert_many(self.connect(), "oi_hist",
                            ["ts_ms", "symbol", "oi", "oi_value", "period"], hist)
            self.connect().commit()
        return len(rows_now) + len(hist)

    def job_ratio(self) -> int:
        """多空比 + taker 主动买卖比 (5m 粒度, 回填 12 条以保证连续性)."""
        syms = self.top_symbols(N_RATIO)
        if not syms:
            return 0
        rows: list[tuple] = []

        def _rows(data: Any) -> list[dict]:
            """只保留带 timestamp 的合法行(交易所限流时可能返回错误体)."""
            if not isinstance(data, list):
                return []
            return [d for d in data if isinstance(d, dict) and "timestamp" in d]

        def one(sym: str):
            a = _rows(self.api.top_long_short_position_ratio(sym, "5m", 12))
            b = _rows(self.api.global_long_short_account_ratio(sym, "5m", 12))
            c = _rows(self.api.taker_long_short_ratio(sym, "5m", 12))
            ta = _rows(self.api.top_long_short_account_ratio(sym, "5m", 12))
            out = []
            for d in ta:
                out.append((parse_binance_ms(d["timestamp"]), sym, "top_account",
                            _f(d.get("longAccount")), _f(d.get("shortAccount")),
                            None, None, None, None, _f(d.get("longShortRatio"))))
            for d in a:
                out.append((parse_binance_ms(d["timestamp"]), sym, "top_position",
                            None, None, _f(d.get("longAccount")), _f(d.get("shortAccount")),
                            None, None, _f(d.get("longShortRatio"))))
            for d in b:
                out.append((parse_binance_ms(d["timestamp"]), sym, "global_account",
                            _f(d.get("longAccount")), _f(d.get("shortAccount")),
                            None, None, None, None, _f(d.get("longShortRatio"))))
            for d in c:
                out.append((parse_binance_ms(d["timestamp"]), sym, "taker",
                            None, None, None, None,
                            _f(d.get("buyVol")), _f(d.get("sellVol")), _f(d.get("buySellRatio"))))
            return out

        for res in self._pmap(one, syms, desc="多空比"):
            rows.extend(res)
        with self._lock:
            upsert_many(self.connect(), "ls_ratio",
                        ["ts_ms", "symbol", "kind", "long_account", "short_account",
                         "long_pos", "short_pos", "buy_ratio", "sell_ratio", "ratio"], rows)
            self.connect().commit()
        return len(rows)

    # 每轮处理的合约数(轮转)与单次拉取条数。
    # 120 条 × 8h ≈ 40 天历史, 足够做「自身历史分位」判断;
    # 8 个合约/轮 × 900 秒 => 约 660 个请求/小时的 1/8, 与其它任务共享 3 rps 节流后约 6 分钟跑完一轮。
    FUNDING_BATCH = 8
    FUNDING_BACKFILL_LIMIT = 120
    FUNDING_INC_LIMIT = 12

    def job_funding_hist(self) -> int:
        """结算资金费率历史(通常 8h 一条).

        ⚠️ 关键: 首次遇到某个 symbol 时**拉全量历史**(limit=1000), 之后只增量补 12 条。
        原因(2026-09-12 实测): 原来的实现每次只取 12 条, 导致每币历史上限就是 12 条,
        根本无法判断「当前费率是否处于该币自身的历史极值」—— 而这正是费率反转策略的核心。
        """
        pool = self.top_symbols(N_MARK)
        if not pool:
            return 0
        n_take = self.FUNDING_BATCH
        if self._funding_cursor >= len(pool):
            self._funding_cursor = 0
        syms = pool[self._funding_cursor:self._funding_cursor + n_take]
        self._funding_cursor = (self._funding_cursor + n_take) % len(pool)
        rows: list[tuple] = []
        def fetch(sym: str):
            limit = (self.FUNDING_INC_LIMIT if sym in self._funding_filled
                     else self.FUNDING_BACKFILL_LIMIT)
            return sym, self.api.funding_rate_history(sym, limit)

        for item in self._pmap(fetch, syms, desc="fundingRate"):
            sym, data = item if isinstance(item, tuple) and len(item) == 2 else (None, None)
            if not sym or not isinstance(data, list):
                continue
            for d in data:
                if not isinstance(d, dict) or "fundingTime" not in d:
                    continue
                rows.append((parse_binance_ms(d["fundingTime"]), sym,
                             _f(d.get("fundingRate")), _f(d.get("markPrice"))))
        if rows:
            with self._lock:
                upsert_many(self.connect(), "funding_hist",
                            ["ts_ms", "symbol", "funding_rate", "mark_price"], rows)
                # 用 upsert 覆盖, 无需降采样; 但删除 400 天前的数据控制体积
                self.connect().commit()
            self._funding_filled.update(syms)
        return len(rows)

    def job_book(self) -> int:
        """盘口价差(流动性/滑点评估)."""
        syms = self.top_symbols(N_BOOK)
        if not syms:
            return 0
        now = int(time.time() // 30 * 30_000)
        data = self.api.book_ticker(syms)
        rows = []
        for d in data:
            bid, ask = _f(d.get("bidPrice")), _f(d.get("askPrice"))
            bq, aq = _f(d.get("bidQty")), _f(d.get("askQty"))
            if not bid or not ask:
                continue
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10_000 if mid else None
            rows.append((now, d["symbol"], bid, ask, bq, aq, spread_bps,
                         (bid * bq) if (bq is not None) else None,
                         (ask * aq) if (aq is not None) else None))
        with self._lock:
            upsert_many(self.connect(), "book_snap",
                        ["ts_ms", "symbol", "bid", "ask", "bid_qty", "ask_qty",
                         "spread_bps", "depth_bid_usd", "depth_ask_usd"], rows)
            self.connect().commit()
        return len(rows)

    def job_basis(self) -> int:
        """期现基差."""
        syms = self.top_symbols(N_BASIS)
        rows = []
        for item in self._pmap(lambda s: (s, self.api.basis(s, "PERPETUAL", "5m", 3)), syms,
                               desc="basis"):
            sym, data = item if isinstance(item, tuple) and len(item) == 2 else (None, None)
            if not sym or not isinstance(data, list):
                continue          # 单个 symbol 失败不影响整轮
            for d in data:
                if not isinstance(d, dict) or "timestamp" not in d:
                    continue      # 交易所限流时可能返回错误体, 直接跳过
                br = _f(d.get("basisRate"))
                rows.append((parse_binance_ms(d["timestamp"]), sym,
                             _f(d.get("futuresPrice")), _f(d.get("indexPrice")),
                             _f(d.get("basis")), br,
                             _f(d.get("annualizedBasisRate")) or ((br or 0.0) * 3 * 365)))
        if rows:
            with self._lock:
                upsert_many(self.connect(), "basis_snap",
                            ["ts_ms", "symbol", "futures_price", "index_price", "basis",
                             "basis_rate", "ann_basis_rate"], rows)
                self.connect().commit()
        return len(rows)

    # ------------------------------------------------------------ 场外数据
    def job_fng(self) -> int:
        """恐贪指数 (alternative.me, 免费公开)."""
        data = self.api.http.get(MACRO_FNG_URL, params={"limit": 3})
        rows = []
        for d in data.get("data", []):
            ts = int(float(d["timestamp"])) * 1000
            rows.append((ts, "fear_greed", float(d["value"]), d.get("value_classification"),
                         "alternative.me"))
        if rows:
            with self._lock:
                upsert_many(self.connect(), "macro",
                            ["ts_ms", "metric", "value", "value_txt", "source"], rows)
                self.connect().commit()
        return len(rows)

    def job_news(self) -> int:
        """免费 RSS 新闻 -> 关键词情绪计数."""
        import re
        import xml.etree.ElementTree as ET

        # 全部为 curl 实测 HTTP 200 的免费源(cryptoslate/reddit-json 已被 Cloudflare 拦截, 剔除)
        feeds = [
            ("cointelegraph", "https://cointelegraph.com/rss"),
            ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
            ("decrypt", "https://decrypt.co/feed"),
            ("theblock", "https://www.theblock.co/rss.xml"),
            ("bitcoinmagazine", "https://bitcoinmagazine.com/feed"),
            ("newsbtc", "https://www.newsbtc.com/feed/"),
            ("cryptonews", "https://crypto.news/feed/"),
            ("ambcrypto", "https://ambcrypto.com/feed/"),
        ]
        rows = []
        for source, url in feeds:
            if self._stop.is_set():
                break
            try:
                text = self.api.http.get_text(url, timeout=15)
                root = ET.fromstring(text)
            except Exception as exc:  # noqa: BLE001
                log.info("RSS %s 不可用: %s", source, exc)
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if not title:
                    continue
                ts = _parse_rfc822(pub) or utc_ms()
                nid = link or f"{source}:{title}"
                rows.append((nid, ts, source, title, link,
                             re.sub("<[^>]+>", "", desc)[:500], "", utc_ms()))
        if rows:
            with self._lock:
                upsert_many(self.connect(), "news",
                            ["id", "ts_ms", "source", "title", "url", "summary", "tags",
                             "fetched_ms"], rows)
                self.connect().commit()
            self._score_news_sentiment()
        return len(rows)

    def _score_news_sentiment(self) -> None:
        """极简关键词情绪: bullish/bearish 计数 (可离线迭代为词表模型)."""
        bullish = ("surge", "rally", "soar", "bullish", "all-time high", "ath", "adopt",
                   "approval", "etf inflow", "partnership", "upgrade", "breakout",
                   "accumulate", "buyback", "listing")
        bearish = ("crash", "plunge", "dump", "bearish", "hack", "exploit", "lawsuit",
                   "sec sues", "ban", "delist", "liquidation", "bankrupt", "outflow",
                   "rug", "scam", "halt")
        since = utc_ms() - 24 * 3600_000
        pos = neg = 0
        try:
            for r in self.connect().execute(
                    "SELECT title, summary FROM news WHERE ts_ms >= ?", (since,)):
                blob = f"{r['title']} {r['summary'] or ''}".lower()
                pos += sum(1 for w in bullish if w in blob)
                neg += sum(1 for w in bearish if w in blob)
        except sqlite3.OperationalError:
            return
        total = pos + neg
        score = 50.0 if total == 0 else round(100.0 * pos / total, 2)
        now = utc_ms()
        with self._lock:
            upsert_many(self.connect(), "macro",
                        ["ts_ms", "metric", "value", "value_txt", "source"],
                        [(now, "news_sentiment", score, f"pos={pos} neg={neg}", "m3dsc_rss")])
            self.connect().commit()

    # ------------------------------------------------------------ 主循环
    def _loop(self, name: str, interval: int, fn: Callable[[], int],
              *, initial_delay: float = 0.0) -> None:
        st = self._stats.setdefault(name, WorkerStat(name=name, interval=interval))
        if initial_delay:
            self._stop.wait(initial_delay)
        while not self._stop.is_set():
            t0 = time.monotonic()
            err = ""
            rows = 0
            try:
                rows = fn() or 0
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                log.exception("[%s] 采集异常", name)
            st.last_dur_ms = int((time.monotonic() - t0) * 1000)
            self._mark_stat(st, rows, err)
            # 任务很长时立即续跑, 否则按间隔等待
            wait = max(1.0, interval - st.last_dur_ms / 1000.0)
            self._stop.wait(wait)

    def _loop_prune(self) -> None:
        """周期清理过期数据. 启动后先立即执行一次, 避免长时间不清理导致磁盘膨胀."""
        st = self._stats.setdefault("prune", WorkerStat(name="prune", interval=1800))
        first = True
        while not self._stop.is_set():
            if not first:
                self._stop.wait(1800)
            first = False
            if self._stop.is_set():
                break
            n = 0
            try:
                with self._lock:
                    for table, keep in RETAIN_MS.items():
                        ts_col = "fetched_ms" if table == "news" else "ts_ms"
                        n += prune(self.connect(), table, ts_col, keep)
                    self.connect().commit()
                self.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._mark_stat(st, n)
            except Exception as exc:  # noqa: BLE001
                self._mark_stat(st, 0, f"{type(exc).__name__}: {exc}")

    def _loop_report(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(300)
            if self._stop.is_set():
                break
            now = utc_ms()
            log.info("=== M3-DSH 采集状态 (%s) 运行 %.1f 分钟 ===",
                     fmt_both(now), (now - self._started_ms) / 60000)
            for st in sorted(self._stats.values(), key=lambda s: s.name):
                log.info("  %-10s 周期%4ds 运行%4d 错误%3d 行/次%6d 耗时%5dms %s",
                         st.name, st.interval, st.runs, st.errors, st.rows, st.last_dur_ms,
                         ("OK " + fmt_both(st.last_ok_ms)) if not st.last_error else "ERR " + st.last_error[:80])
            self._dump_status()

    def _dump_status(self) -> None:
        try:
            (self.live_dir / "collector_status.json").write_text(json.dumps({
                "generated_ms": utc_ms(),
                "uptime_min": round((utc_ms() - self._started_ms) / 60000, 2),
                "n_tradable": len(self.tradable),
                "workers": {s.name: {"interval": s.interval, "runs": s.runs, "errors": s.errors,
                                     "rows": s.rows, "last_ok_ms": s.last_ok_ms,
                                     "last_error": s.last_error} for s in self._stats.values()},
                "http": self.api.http.stats,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("状态落盘失败: %s", exc)

    def run(self) -> int:
        self.install_signals()
        self.connect()
        log.info("M3-DSH 采集器启动 (%s)", fmt_both(utc_ms()))
        log.info("数据库: %s", self.db_path)

        # 首轮同步执行关键任务, 保证 watchlist 尽快产出
        for name, fn in (("meta", lambda: (self.refresh_meta(force=True), 0)[1]),
                         ("ticker", self.job_ticker), ("mark", self.job_mark)):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.error("首轮 %s 失败: %s", name, exc)

        threads: list[threading.Thread] = []
        plan: list[tuple[str, int, Callable[[], int], float]] = [
            ("ticker", self.s.interval_ticker, self.job_ticker, 0.0),
            ("mark", self.s.interval_mark, self.job_mark, 5.0),
            ("book", 120, self.job_book, 10.0),
            ("oi", self.s.interval_oi, self.job_oi, 15.0),
            ("ratio", self.s.interval_ratio, self.job_ratio, 20.0),
            ("basis", self.s.interval_klines, self.job_basis, 30.0),
            # ⚠️ 费率历史必须保留: 「当前费率是否处于该币自身历史极值」是费率反转策略的
            #    核心输入。此前为了省请求把它整条删掉, 导致分位数永远无法计算(2026-09-12)。
            #    现在改为轮转分批(每轮 8 个合约 × 最多 120 条)以控制请求量。
            ("funding_hist", 900, self.job_funding_hist, 35.0),
        ]
        if self.enable_fng:
            plan.append(("fng", self.s.interval_fng, self.job_fng, 8.0))
        if self.enable_news:
            plan.append(("news", self.s.interval_news, self.job_news, 12.0))

        for name, interval, fn, delay in plan:
            t = threading.Thread(target=self._loop, args=(name, interval, fn),
                                 kwargs={"initial_delay": delay}, name=f"w-{name}", daemon=True)
            t.start()
            threads.append(t)
        for extra in (self._loop_prune, self._loop_report):
            t = threading.Thread(target=extra, name="w-housekeep", daemon=True)
            t.start()
            threads.append(t)

        self._dump_status()
        while not self._stop.is_set():
            self._stop.wait(1.0)
        for t in threads:
            t.join(timeout=30)
        self._dump_status()   # 退出前再落盘一次(SIGTERM 场景)
        if self.conn:
            self.conn.commit()
            self.conn.close()
        log.info("采集器已停止.")
        return 0


def _parse_rfc822(value: str) -> int | None:
    if not value:
        return None
    import email.utils
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        import datetime as _dt
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M3-DSH Binance 多源数据采集器")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出(用于自检)")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--no-fng", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    SETTINGS.ensure_dirs()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        handlers=[StreamHandler(sys.stdout),
                  logging.FileHandler(SETTINGS.log_dir / "collector.log", encoding="utf-8")],
    )
    c = MarketCollector(parallel=args.parallel, enable_fng=not args.no_fng,
                        enable_news=not args.no_news)
    if args.once:
        c.connect()
        for name, fn in (("meta", lambda: (c.refresh_meta(force=True), 0)[1]),
                         ("ticker", c.job_ticker), ("mark", c.job_mark), ("book", c.job_book),
                         ("oi", c.job_oi), ("ratio", c.job_ratio), ("basis", c.job_basis)):
            try:
                n = fn()
                log.info("[once] %s -> %s 行", name, n)
            except Exception as exc:  # noqa: BLE001
                log.error("[once] %s 失败: %s", name, exc)
        if not args.no_fng:
            try:
                log.info("[once] fng -> %s 行", c.job_fng())
            except Exception as exc:  # noqa: BLE001
                log.error("[once] fng 失败: %s", exc)
        c._dump_status()
        return 0
    return c.run()


if __name__ == "__main__":
    raise SystemExit(main())
