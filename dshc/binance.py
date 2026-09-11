"""Binance USD-M 永续合约公共数据客户端 (无需 API Key).

参考限额(M3-DSH 采集器预算): ticker/24hr 单次 weight=40(全市场), 其余 per-symbol
接口多为 1~5 weight, IP 上限 2400/min。采集器每轮打满一次即可, 远低于限额。

时间: Binance 所有 timestamp 均为 **UTC epoch milliseconds**。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from .httpx import Client, HttpError, RateLimiter
from .timeutil import parse_binance_ms, utc_ms

log = logging.getLogger("dshc.binance")

FAPI = "https://fapi.binance.com"
FAPI_TESTNET = "https://testnet.binancefuture.com"

# 稳定币/指数类合约, 永不作为交易标的
NON_TRADABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "AEUR", "USDTB",
    "USD1", "XUSD", "EURI", "BFUSD", "PAXG",
}

# 主流大市值: 允许作为"锚", 但在涨幅榜策略里通常排在后面
MAJOR_BASES = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TRX", "LTC"}

# 交易所特殊品种(1000X 等)保留, 但记录


class BinanceFutures:
    def __init__(self, proxy: str = "", base_url: str = FAPI, rps: float = 8.0) -> None:
        self.base = base_url.rstrip("/")
        self.limiter = RateLimiter(rps, burst=64)
        # 桶容量给足, 保证「全市场 ticker(weight=40)」这类大请求也能得到满速配额;
        # 同时把限速器注入 Client, 让 429 熔断对所有并发线程生效
        self.http = Client(proxy=proxy, timeout=20.0, retries=4, limiter=self.limiter)

    # ------------------------------------------------------------ 元数据
    def exchange_info(self) -> dict[str, Any]:
        self.limiter.acquire(1)
        return self.http.get(f"{self.base}/fapi/v1/exchangeInfo")

    def perp_symbols(self, *, quote: str = "USDT") -> list[dict[str, Any]]:
        """返回可交易 USDT 永续合约列表(已过滤稳定币/下架)."""
        info = self.exchange_info()
        out: list[dict[str, Any]] = []
        for s in info.get("symbols", []):
            if s.get("quoteAsset") != quote:
                continue
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset", "")
            if base in NON_TRADABLE_BASES:
                continue
            out.append(s)
        return out

    # ------------------------------------------------------------ 行情
    def ticker_24hr(self, symbols: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """24h 行情. 不传 symbols 时返回全市场(weight=40)."""
        self.limiter.acquire(40 if not symbols else 2)
        if symbols:
            params = {"symbols": _json_list(symbols)}
        else:
            params = None
        data = self.http.get(f"{self.base}/fapi/v1/ticker/24hr", params=params)
        return data if isinstance(data, list) else [data]

    def book_ticker(self, symbols: Sequence[str] | None = None) -> list[dict[str, Any]]:
        self.limiter.acquire(5 if not symbols else 2)
        params = {"symbols": _json_list(symbols)} if symbols else None
        data = self.http.get(f"{self.base}/fapi/v1/ticker/bookTicker", params=params)
        return data if isinstance(data, list) else [data]

    def klines(self, symbol: str, interval: str = "1h", limit: int = 200,
               start_ms: int | None = None, end_ms: int | None = None) -> list[list[Any]]:
        self.limiter.acquire(5)
        return self.http.get(f"{self.base}/fapi/v1/klines", params={
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_ms, "endTime": end_ms,
        })

    # ------------------------------------------------------------ 资金费率
    def premium_index(self, symbols: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """最新标记价 + 资金费率 + 下次结算时间.

        lastFundingRate 为「当前周期」费率, 正数=多头付给空头。
        """
        self.limiter.acquire(10 if not symbols else 1)
        params = {"symbols": _json_list(symbols)} if symbols else None
        data = self.http.get(f"{self.base}/fapi/v1/premiumIndex", params=params)
        return data if isinstance(data, list) else [data]

    def funding_rate_history(self, symbol: str, limit: int = 100,
                             start_ms: int | None = None) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/fapi/v1/fundingRate", params={
            "symbol": symbol, "limit": limit, "startTime": start_ms,
        })
        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------ 持仓量
    def open_interest(self, symbol: str) -> dict[str, Any]:
        self.limiter.acquire(1)
        return self.http.get(f"{self.base}/fapi/v1/openInterest", params={"symbol": symbol})

    def open_interest_hist(self, symbol: str, period: str = "5m",
                           limit: int = 100) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/openInterestHist", params={
            "symbol": symbol, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------ 多空比
    def top_long_short_account_ratio(self, symbol: str, period: str = "5m",
                                     limit: int = 30) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/topLongShortAccountRatio", params={
            "symbol": symbol, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]

    def top_long_short_position_ratio(self, symbol: str, period: str = "5m",
                                      limit: int = 30) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/topLongShortPositionRatio", params={
            "symbol": symbol, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]

    def global_long_short_account_ratio(self, symbol: str, period: str = "5m",
                                        limit: int = 30) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/globalLongShortAccountRatio", params={
            "symbol": symbol, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]

    def taker_long_short_ratio(self, symbol: str, period: str = "5m",
                               limit: int = 30) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/takerlongshortRatio", params={
            "symbol": symbol, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------ 基差/其他
    def basis(self, pair: str, contract_type: str = "PERPETUAL", period: str = "5m",
              limit: int = 30) -> list[dict[str, Any]]:
        self.limiter.acquire(1)
        data = self.http.get(f"{self.base}/futures/data/basis", params={
            "pair": pair, "contractType": contract_type, "period": period, "limit": limit,
        })
        return data if isinstance(data, list) else [data]


def _json_list(symbols: Iterable[str]) -> str:
    import json
    return json.dumps(list(symbols), separators=(",", ":"))


def parse_ticker_row(t: dict[str, Any], ts_ms: int) -> tuple:
    """Binance 24hr ticker -> ticker_snap 行."""
    return (
        ts_ms,
        t["symbol"],
        _f(t.get("lastPrice")),
        _f(t.get("priceChangePercent")),
        _f(t.get("quoteVolume")),
        _f(t.get("volume")),
        int(_f(t.get("count"))) if t.get("count") is not None else None,
        _f(t.get("highPrice")),
        _f(t.get("lowPrice")),
        _f(t.get("openPrice")),
        _f(t.get("weightedAvgPrice")),
    )


def parse_premium_row(p: dict[str, Any], ts_ms: int) -> tuple:
    return (
        ts_ms,
        p["symbol"],
        _f(p.get("markPrice")),
        _f(p.get("indexPrice")),
        _f(p.get("lastFundingRate")),
        parse_binance_ms(p.get("nextFundingTime", 0)) if p.get("nextFundingTime") else None,
        _f(p.get("interestRate")),
        int(p.get("fundingIntervalHours") or 8) if p.get("fundingIntervalHours") else 8,
        _f(p.get("estimatedSettlePrice")),
    )


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
