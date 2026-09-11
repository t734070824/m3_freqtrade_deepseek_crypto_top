"""极简 HTTP 客户端: 自动重试/退避/代理/限速, 只依赖标准库.

为什么不用 requests: 采集容器镜像要尽量小且离线可构建;
标准库 urllib 足够覆盖 Binance REST + RSS + JSON 公开接口。
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("dshc.http")

_UA = "m3dsc-collector/0.1 (+freqtrade-dryrun; contact: local)"
_SSL_CTX = ssl.create_default_context()


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} {url} :: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class MissingDependency(RuntimeError):
    pass


try:  # 可选: 支持 socks5h 代理
    import socks  # type: ignore
    import socket

    _HAS_SOCKS = True
except Exception:  # pragma: no cover
    _HAS_SOCKS = False


def _install_socks(proxy: str) -> None:
    if not proxy:
        return
    if not _HAS_SOCKS:
        raise MissingDependency("需要 PySocks 才能使用 socks5 代理: pip install PySocks")
    parsed = urllib.parse.urlparse(proxy)
    if parsed.scheme not in ("socks5", "socks5h", "socks4"):
        raise MissingDependency(f"不支持的代理协议: {parsed.scheme}")
    socks.set_default_proxy(socks.SOCKS5 if "5" in parsed.scheme else socks.SOCKS4,
                            parsed.hostname, parsed.port or 1080,
                            rdns=(parsed.scheme == "socks5h"))
    socket.socket = socks.socksocket  # type: ignore[misc]


class Client:
    """带重试的 GET 客户端."""

    def __init__(self, proxy: str = "", timeout: float = 15.0,
                 retries: int = 4, backoff: float = 0.8, obey_retry_after: bool = True,
                 limiter: "RateLimiter | None" = None) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.obey_retry_after = obey_retry_after
        # 由调用方注入共享限速器, 使 429 熔断对并发线程可见
        self.limiter = limiter
        self._disabled = False
        self._stats: dict[str, int] = {"ok": 0, "err": 0, "retry": 0}
        self.last_headers: dict[str, str] = {}
        if proxy:
            _install_socks(proxy)

    # ---------------------------------------------------------------- core
    def get(self, url: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None, timeout: float | None = None) -> Any:
        """GET 并解析 JSON."""
        body = self.get_text(url, params=params, headers=headers, timeout=timeout)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise HttpError(0, url, f"JSON 解析失败: {exc}; 前300字符={body[:300]}") from exc

    def get_text(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None,
                 timeout: float | None = None) -> str:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        hdrs = {"User-Agent": _UA, "Accept": "*/*", "Accept-Encoding": "gzip",
                "Connection": "close"}
        if headers:
            hdrs.update(headers)

        if self.limiter is not None:
            self.limiter.maybe_restore()
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=hdrs, method="GET")
                with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                            context=_SSL_CTX) as resp:  # noqa: S310
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    # 记录 Binance 限速监控头(X-MBX-USED-WEIGHT-1M 等)
                    self.last_headers = {k: v for k, v in resp.headers.items()
                                         if k.lower().startswith("x-mbx")}
                    self._stats["ok"] += 1
                    return raw.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_exc = HttpError(exc.code, url, body)
                # 3xx: urllib 不自动处理 308/307 -> 手动跟随 Location
                if exc.code in (301, 302, 303, 307, 308):
                    loc = exc.headers.get("Location")
                    if loc and attempt < self.retries:
                        url = urllib.parse.urljoin(url, loc)
                        self._stats["retry"] += 1
                        continue
                # 429/418 -> 遵守 Retry-After
                if exc.code in (429, 418):
                    wait = max(self.backoff * (2 ** attempt), 5.0 if attempt == 0 else 10.0)
                    if self.obey_retry_after:
                        try:
                            wait = max(wait, float(exc.headers.get("Retry-After", 0) or 0))
                        except (TypeError, ValueError):
                            pass
                    # 熔断: 通知共享限速器整体降速一段时间
                    if self.limiter is not None:
                        self.limiter.penalize(120.0, 0.25)
                    self._sleep(wait)
                    continue
                if 400 <= exc.code < 500:
                    self._stats["err"] += 1
                    raise last_exc from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc  # type: ignore[assignment]

            self._stats["retry"] += 1
            self._sleep(self.backoff * (2 ** attempt) + random.uniform(0, 0.25 * self.backoff))

        self._stats["err"] += 1
        raise RuntimeError(f"GET 失败(重试{self.retries}次): {url} :: {last_exc}") from last_exc

    def _sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 30.0))

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class RateLimiter:
    """令牌桶: 控制每秒请求数, 保护 Binance weight 限额.

    注意(2024-09 血泪): 单次请求所需令牌数可能大于桶容量(例如全市场
    ticker/24hr 一次消耗 weight=40, 而桶只有 8 个), 必须把请求量夹到
    容量以内, 否则 acquire() 会陷入「永远攒不够」的死循环。
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None) -> None:
        self.base_rate = max(float(rate_per_sec), 0.05)
        self.rate = self.base_rate
        self.capacity = max(1, int(burst or max(1.0, rate_per_sec)))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._penalty_until = 0.0

    def penalize(self, seconds: float, factor: float = 0.25) -> None:
        """收到 429/418 后的熔断: 降低速率一段时间, 避免继续激怒交易所.

        背景: 币安 IP 限额是「每 IP 2400 weight/min」, 同一台机器上的其它
        freqtrade 实例也在消耗同一个配额, 所以必须让出余量、主动降速。
        """
        now = time.monotonic()
        self._penalty_until = max(self._penalty_until, now + seconds)
        self.rate = max(self.base_rate * factor, 0.1)

    def maybe_restore(self) -> None:
        if self._penalty_until and time.monotonic() > self._penalty_until:
            self.rate = self.base_rate
            self._penalty_until = 0.0

    def acquire(self, n: float = 1.0) -> None:
        n = min(max(float(n), 0.0), float(self.capacity))   # 关键: 夹到容量上限
        if n <= 0:
            return
        while True:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return
            time.sleep(max((n - self._tokens) / self.rate, 0.005))
