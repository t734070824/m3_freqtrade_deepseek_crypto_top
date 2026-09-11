"""SQLite 存储层 (标准库 sqlite3, WAL 模式, 支持多进程并发写).

设计要点
--------
* 时间一律 UTC 毫秒时间戳 (ts_ms)。展示时由 UI/脚本显式换算为北京时间(UTC+8)。
* 所有写操作使用 UPSERT, 保证采集器重启幂等。
* WAL + busy_timeout 允许采集器/看板/策略并行访问。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

log = logging.getLogger("dshc.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS perp_meta (
    symbol                  TEXT PRIMARY KEY,
    base                    TEXT,
    quote                   TEXT,
    contract_type           TEXT,
    status                  TEXT,
    onboard_ms              INTEGER,
    delivery_ms             INTEGER,
    price_precision         INTEGER,
    qty_precision           INTEGER,
    tick_size               REAL,
    min_qty                 REAL,
    min_notional            REAL,
    last_funding_rate       REAL,
    funding_interval_hours  INTEGER,
    updated_ms              INTEGER
);

CREATE TABLE IF NOT EXISTS ticker_snap (
    ts_ms            INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    price            REAL,
    price_change_pct REAL,
    quote_vol        REAL,
    base_vol         REAL,
    trade_count      INTEGER,
    high_24h         REAL,
    low_24h          REAL,
    open_24h         REAL,
    weighted_avg     REAL,
    PRIMARY KEY (ts_ms, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ticker_sym_ts ON ticker_snap(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS perp_mark (
    ts_ms                    INTEGER NOT NULL,
    symbol                   TEXT NOT NULL,
    mark_price               REAL,
    index_price              REAL,
    last_funding_rate        REAL,
    next_funding_time_ms     INTEGER,
    interest_rate            REAL,
    funding_interval_hours   INTEGER,
    pred_funding_rate        REAL,
    PRIMARY KEY (ts_ms, symbol)
);
CREATE INDEX IF NOT EXISTS idx_mark_sym_ts ON perp_mark(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS oi_now (
    ts_ms    INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    oi       REAL,
    oi_value REAL,
    PRIMARY KEY (ts_ms, symbol)
);
CREATE INDEX IF NOT EXISTS idx_oi_sym_ts ON oi_now(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS oi_hist (
    ts_ms    INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    oi       REAL,
    oi_value REAL,
    period   TEXT,
    PRIMARY KEY (ts_ms, symbol)
);

CREATE TABLE IF NOT EXISTS funding_hist (
    ts_ms            INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    funding_rate     REAL,
    mark_price       REAL,
    PRIMARY KEY (ts_ms, symbol)
);

CREATE TABLE IF NOT EXISTS ls_ratio (
    ts_ms        INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    long_account REAL,
    short_account REAL,
    long_pos     REAL,
    short_pos    REAL,
    buy_ratio    REAL,
    sell_ratio   REAL,
    ratio        REAL,
    PRIMARY KEY (ts_ms, symbol, kind)
);
CREATE INDEX IF NOT EXISTS idx_ls_sym_kind_ts ON ls_ratio(symbol, kind, ts_ms DESC);

-- 说明: 早期设计里还有 univ / watchlist 两张表, 实际从未写入 —— 榜单与打分的
-- 快照统一由 rank_snap 承担(5 分钟桶), 因此已删除, 避免出现「永远 0 行」的死表。

CREATE TABLE IF NOT EXISTS macro (
    ts_ms     INTEGER NOT NULL,
    metric    TEXT NOT NULL,
    value     REAL,
    value_txt TEXT,
    source    TEXT NOT NULL,
    PRIMARY KEY (ts_ms, metric, source)
);
CREATE INDEX IF NOT EXISTS idx_macro_metric_ts ON macro(metric, ts_ms DESC);

CREATE TABLE IF NOT EXISTS news (
    id         TEXT PRIMARY KEY,
    ts_ms      INTEGER,
    source     TEXT,
    title      TEXT,
    url        TEXT,
    summary    TEXT,
    tags       TEXT,
    fetched_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts_ms DESC);

CREATE TABLE IF NOT EXISTS collector_status (
    collector   TEXT PRIMARY KEY,
    last_ok_ms  INTEGER,
    last_run_ms INTEGER,
    last_error  TEXT,
    rows_last   INTEGER,
    runs        INTEGER,
    errors      INTEGER
);

CREATE TABLE IF NOT EXISTS book_snap (
    ts_ms        INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    bid          REAL,
    ask          REAL,
    bid_qty      REAL,
    ask_qty      REAL,
    spread_bps   REAL,
    depth_bid_usd REAL,
    depth_ask_usd REAL,
    PRIMARY KEY (ts_ms, symbol)
);
CREATE INDEX IF NOT EXISTS idx_book_sym_ts ON book_snap(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS basis_snap (
    ts_ms        INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    futures_price REAL,
    index_price  REAL,
    basis        REAL,
    basis_rate   REAL,
    ann_basis_rate REAL,
    PRIMARY KEY (ts_ms, symbol)
);

-- 榜单与打分快照(5 分钟粒度), 用于分析名次稳定性 / 打分与收益的相关性
CREATE TABLE IF NOT EXISTS rank_snap (
    ts_ms       INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    rank        INTEGER,
    score       REAL,
    change_24h  REAL,
    funding_ann REAL,
    oi_chg_1h   REAL,
    ls_ratio    REAL,
    taker_ratio REAL,
    spread_bps  REAL,
    tags        TEXT,
    PRIMARY KEY (ts_ms, symbol)
);
CREATE INDEX IF NOT EXISTS idx_rank_sym_ts ON rank_snap(symbol, ts_ms DESC);

"""


def connect(path: str | Path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    """打开连接并设置 WAL/超时."""
    p = Path(path)
    if readonly:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=timeout, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_many(conn: sqlite3.Connection, table: str, columns: Sequence[str],
                rows: Iterable[Sequence[Any]], *, chunk: int = 400) -> int:
    """批量 UPSERT, 返回写入行数."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return 0
    cols = ",".join(columns)
    placeholders = ",".join(["?"] * len(columns))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT DO UPDATE SET " + \
          ",".join(f"{c}=excluded.{c}" for c in columns)
    total = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        conn.executemany(sql, batch)
        total += len(batch)
    return total


def heartbeat(conn: sqlite3.Connection, collector: str, *, rows: int = 0, error: str | None = None,
              now_ms: int | None = None) -> None:
    """记录采集器心跳."""
    from .timeutil import utc_ms
    now = now_ms if now_ms is not None else utc_ms()
    conn.execute(
        """INSERT INTO collector_status(collector,last_ok_ms,last_run_ms,last_error,rows_last,runs,errors)
           VALUES(?,?,?,?,?,1,?)
           ON CONFLICT(collector) DO UPDATE SET
             last_run_ms=excluded.last_run_ms,
             last_ok_ms=CASE WHEN excluded.last_error IS NULL THEN excluded.last_ok_ms ELSE collector_status.last_ok_ms END,
             last_error=excluded.last_error,
             rows_last=excluded.rows_last,
             runs=collector_status.runs+1,
             errors=collector_status.errors+CASE WHEN excluded.last_error IS NULL THEN 0 ELSE 1 END""",
        (collector, now, now, error, rows, 0 if error is None else 1),
    )


def prune(conn: sqlite3.Connection, table: str, ts_col: str, keep_ms: int,
          *, now_ms: int | None = None) -> int:
    """删除过旧数据, 返回删除行数."""
    from .timeutil import utc_ms
    cutoff = (now_ms if now_ms is not None else utc_ms()) - keep_ms
    cur = conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,))
    return cur.rowcount or 0