"""时间工具: 统一 UTC 毫秒时间戳, 展示时显式标注时区.

约定
----
* 存储: 一律 UTC epoch milliseconds (int)  —— 无歧义
* 展示: 必须显式标注. 使用 fmt() -> "2025-05-01 12:00:00 UTC"
                               fmt_cn() -> "2025-05-01 20:00:00 北京时间(UTC+8)"
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))  # 北京时间 UTC+8
UTC = timezone.utc


def utc_ms() -> int:
    """当前 UTC 毫秒时间戳."""
    return int(time.time() * 1000)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_cst() -> datetime:
    return datetime.now(CST)


def utc_seconds() -> int:
    return int(time.time())


def ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def minutes_ago_ms(minutes: float) -> int:
    return utc_ms() - int(minutes * 60_000)


def fmt(ms: int | float) -> str:
    """UTC 显式标注格式."""
    return ms_to_dt(ms).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def fmt_cn(ms: int | float) -> str:
    """北京时间(UTC+8) 显式标注格式."""
    return datetime.fromtimestamp(ms / 1000.0, CST).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间(UTC+8)"


def fmt_both(ms: int | float) -> str:
    return f"{fmt_cn(ms)} / {fmt(ms)}"


def iso_utc(ms: int | float) -> str:
    return ms_to_dt(ms).isoformat().replace("+00:00", "Z")


def parse_binance_ms(value) -> int:
    """Binance 返回的毫秒时间戳可能是 str/int/float."""
    return int(float(value))
