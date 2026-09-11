"""集中配置: 环境变量 -> 强类型配置对象."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("DSHC_ROOT", Path(__file__).resolve().parent.parent))


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None or v == "" else v


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    tag: str = field(default_factory=lambda: _env("DSHC_PREFIX", "m3dsc"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DSHC_DATA_DIR", str(ROOT / "data"))))
    log_dir: Path = field(default_factory=lambda: Path(_env("DSHC_LOG_DIR", str(ROOT / "logs"))))
    proxy: str = field(default_factory=lambda: _env("DSHC_PROXY", ""))

    # 场外数据开关
    enable_fng: bool = field(default_factory=lambda: _env_bool("DSHC_ENABLE_FNG", True))
    enable_news: bool = field(default_factory=lambda: _env_bool("DSHC_ENABLE_NEWS", True))
    enable_orderbook: bool = field(default_factory=lambda: _env_bool("DSHC_ENABLE_ORDERBOOK", True))

    # 采集频率(秒)
    interval_ticker: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_TICKER", 60))
    interval_mark: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_MARK", 60))
    interval_oi: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_OI", 180))
    interval_ratio: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_RATIO", 300))
    interval_fng: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_FNG", 900))
    interval_news: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_NEWS", 600))
    interval_klines: int = field(default_factory=lambda: _env_int("DSHC_INTERVAL_KLINES", 300))

    # 涨幅榜过滤阈值
    min_quote_vol_usdt: float = field(default_factory=lambda: _env_float("DSHC_MIN_QUOTE_VOL", 30_000_000.0))
    top_n: int = field(default_factory=lambda: _env_int("DSHC_TOP_N", 40))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "m3dsc_market.db"

    @property
    def dashboard_db(self) -> Path:
        return self.data_dir / "m3dsc_dashboard.db"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.log_dir, self.data_dir / "raw"):
            p.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
