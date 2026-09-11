# 数据字典 (M3-DSH)

数据库: `data/m3dsc_market.db` (SQLite, WAL 模式)
**所有时间字段均为 UTC 毫秒时间戳** (`ts_ms`)。展示时由 `dshc/timeutil.py` 换算为
`北京时间(UTC+8)` 与 `UTC` 双标注。

## 1. perp_meta — 合约元数据

| 字段 | 类型 | 说明 |
|---|---|---|
| symbol | TEXT PK | 合约代码, 如 `BTCUSDT` |
| base / quote | TEXT | 基础/计价资产 |
| contract_type | TEXT | `PERPETUAL` |
| status | TEXT | `TRADING` |
| onboard_ms | INTEGER | 上线时间 (UTC ms) |
| price_precision / qty_precision | INTEGER | 精度 |
| tick_size / min_qty / min_notional | REAL | 交易过滤器 |
| funding_interval_hours | INTEGER | **资金费率结算周期**(8 或 4 小时) |
| updated_ms | INTEGER | 本行刷新时间 (UTC ms) |

## 2. ticker_snap — 24h 行情快照 (60s)

| 字段 | 说明 |
|---|---|
| ts_ms | 采集时间, 按分钟对齐 (UTC ms) |
| price / price_change_pct | 最新价 / **24h 涨跌幅%** |
| quote_vol / base_vol | 24h 成交额(USDT) / 成交量 |
| trade_count | 24h 成交笔数 |
| high_24h / low_24h / open_24h | 24h 高低开 |
| weighted_avg | 加权均价 |

**主键**: `(ts_ms, symbol)`；索引: `(symbol, ts_ms DESC)`

## 3. perp_mark — 标记价与资金费率 (60s)

| 字段 | 说明 |
|---|---|
| mark_price / index_price | 标记价 / 指数价 |
| **last_funding_rate** | **当前周期资金费率**(小数, 正数=多头付给空头) |
| **next_funding_time_ms** | **下次结算时间** (UTC ms) |
| interest_rate | 基础利率(通常 0.0001) |
| funding_interval_hours | 结算周期(小时) |

> 年化换算: `annualized = last_funding_rate × (24 / funding_interval_hours) × 365`

## 4. oi_now / oi_hist — 持仓量

`oi_now` (180s): `ts_ms, symbol, oi` (币本位数量), `oi_value` (USDT 名义价值 = oi × mark_price)
`oi_hist` (300s): 交易所 5m 粒度历史 `sumOpenInterest` / `sumOpenInterestValue`

**用途**: 持仓量上升 + 价格上升 = 增量资金进场；价格上升但 OI 下降 = 空头回补(不可持续)。

## 5. funding_hist — 结算资金费率历史 (1h 刷新)

| 字段 | 说明 |
|---|---|
| ts_ms | **结算时刻** (UTC ms) |
| funding_rate | 该期实际结算费率 |
| mark_price | 结算时的标记价 |

**注意**: 策略里使用该序列时，必须按「结算时刻 + 1ms」生效（下一根 K 线才可见），
否则会把未来信息带入当前 K 线（当前实现见 `M3GainersTrend._attach_external`）。

## 6. ls_ratio — 多空比与主动买卖比 (300s, 交易所 5m 粒度)

| kind | 含义 | 有效字段 |
|---|---|---|
| `top_account` | 大户账户数多空比 | long_account / short_account / ratio |
| `top_position` | **大户持仓量多空比** | long_pos / short_pos / ratio |
| `global_account` | 全局账户多空比(散户情绪) | long_account / short_account / ratio |
| `taker` | 主动买卖量比 | buy_ratio(buyVol) / sell_ratio(sellVol) / ratio(buySellRatio) |

## 7. book_snap — 盘口 (120s)

`bid, ask, bid_qty, ask_qty, spread_bps`(价差基点), `depth_bid_usd, depth_ask_usd`

## 8. basis_snap — 期现基差 (300s)

`futures_price, index_price, basis, basis_rate, ann_basis_rate`(年化)

## 9. macro — 场外宏观指标

| metric | 来源 | 值域 | 说明 |
|---|---|---|---|
| `fear_greed` | alternative.me | 0~100 | 0=极度恐慌, 100=极度贪婪, 每日 00:00 UTC 更新 |
| `news_sentiment` | 8 家 RSS 关键词统计 | 0~100 | 24h 内正面/负面关键词占比 |

## 10. news — 新闻条目

`id, ts_ms(发布时间 UTC ms), source, title, url, summary, fetched_ms`

## 10.5 rank_snap — 榜单与打分快照 (5 分钟粒度)

`ts_ms`(5 分钟对齐), `symbol, rank, score, change_24h, funding_ann, oi_chg_1h, ls_ratio, taker_ratio, spread_bps, tags`

**用途**: 分析榜单名次稳定性、以及「打分 vs 未来真实收益」的相关性
(`scripts/dshc_analyze.py alpha | stability | bench`)。保留 30 天。

> 另: 早期设计中的 `univ` / `watchlist` 两张表从未写入, 已删除 —— 该职责由 `rank_snap` 承担。

## 11. collector_status — 采集器心跳

`collector, last_ok_ms, last_run_ms, last_error, rows_last, runs, errors`

## 12. 数据保留策略

| 表 | 保留 |
|---|---|
| ticker_snap / perp_mark / oi_now / oi_hist / basis_snap | 4 天 |
| ls_ratio | 7 天 |
| book_snap | 12 小时 |
| funding_hist | 30 天 |
| news | 14 天 |

## 13. 文件产出 (data/live/)

| 文件 | 内容 |
|---|---|
| `watchlist.json` | 完整候选池 + 打分明细 + 宏观指标 (每分钟原子替换) |
| `pairs.json` | freqtrade `RemotePairList` 契约: `{pairs:[...], refresh_period:60}` |
| `gainers.txt` | 纯涨幅榜前 120 名(流动性过滤后) |
| `collector_status.json` | 各采集任务运行状态(供看板/自检) |
