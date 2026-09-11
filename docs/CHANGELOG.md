# 变更记录 (北京时间 UTC+8 标注)

## 2026-09-11 22:55 北京时间(14:55 UTC) — v0.2.0 限流修复与运维完善

### ⚠️ 严重问题修复：币安 429 限流
**现象**：freqtrade 日志出现 `binance 429 Too Many Requests {"code":-1003,"msg":"Too many requests; current limit of IP(206.237.5.124) is 2400 requests per minute"}`，`fetch_funding_rate()` 连续失败。

**根因**：策略在每个分析轮次对白名单内**每个交易对**都调用一次 `self.dp.funding_rate(pair)`（ccxt premiumIndex），55 个交易对 × 每 5 秒一轮 ≈ **660 请求/分钟**；叠加采集器与同主机另外几个 freqtrade 实例，总请求量突破「每 IP 2400 weight/min」的共享配额。

**修复（四处）**：
1. 策略侧加 **300 秒 TTL 缓存**（`_refresh_live_funding`）。资金费率在结算周期（8h）内才变化，5 分钟缓存足够且安全 —— 请求量降低约 60 倍；请求失败时沿用旧缓存而不是清零。
2. 采集器 `BinanceFutures(rps=8.0 → 3.0)`，并新增跨线程共享的最小间隔限速 `_throttle()`，把 `/futures/data/*` 家族与 REST 请求一起纳入节流。
3. HTTP 客户端新增**熔断**：收到 429/418 时通知共享限速器整体降速到 25% 并保持 120 秒，同时把退避下限提高到 5~10 秒（原来仅 0.8~1.6 秒，等于继续激怒交易所）。
4. 关闭冗余的独立 `/fapi/v1/fundingRate` 轮询任务 —— 全市场 `premiumIndex` 已包含 `lastFundingRate` 与 `nextFundingTime`。

### 其它改进
* 看板新增 `/api/closed`（已平仓明细）、`/api/pairlist`（RemotePairList 数据源）、按日收益表。
* 采集器新增 `rank_snap` 表（5 分钟粒度的榜单与打分数快照），用于「打分 vs 真实收益」归因与名次稳定性分析。
* 策略新增 `confirm_trade_exit`：记录平仓时刻并启用 **60 分钟再入场冷却**，防止同一标的被反复插针收割。
* 移除镜像内重复的策略副本（造成 freqtrade `DUPLICATE NAME` 告警）。
* 新增 `scripts/dshc_report.py` 报告工具与重写的 `dshc-verify.sh`（全部通过）。

---

## 2026-09-11 (北京时间 22:30 前后) — v0.1.0 首次可运行版本

### 新增
* **采集层** `m3dsc-market-collector`：9 路并发采集
  (ticker/mark/book/oi/ratio/funding_hist/basis/fng/news)，SQLite WAL 时序库 + 数据保留策略。
* **打分引擎** `dshc/screener.py`：9 因子加权打分，输出 `score ∈ [-100,+100]`；
  涨幅榜前 40 + 跌幅榜前 10 + 主流锚点构成候选池。
* **交易策略** `M3GainersTrend`：4h/1h/5m 三周期共振，多空双向，
  资金费率四处强制约束，ATR 跟踪 + 阶梯分批收割，风险预算定仓。
* **看板** `m3dsc-dashboard`：候选池/持仓/涨幅榜/采集健康，60 秒自刷新。
* **编排**：docker-compose 三服务，容器名前缀 `m3dsc`。
* **运维脚本**：`dshc-init/up/down/status/logs/shell/verify`。

### 修复（开发过程中踩到的真实坑）
1. **限速器死锁**：令牌桶容量(8) < 单次大请求所需令牌(全市场 ticker weight=40)，
   `acquire()` 陷入永不满足的循环 → 采集器假死 15 分钟。修复：请求量夹到桶容量并加大 burst。
2. **freqtrade 2026.x 无 funding_rate 数据列**：旧版本可用的 `dataframe['funding_rate']`
   已被移除。改为 `self.dp.funding_rate(pair)`（实时）+ 自建 `funding_hist` 序列（历史）。
3. **freqtrade 2026.x 无法注册自定义 pairlist**：`pairlists[].method` 是枚举白名单校验。
   改用官方 `RemotePairList` + 本地产 `file:///workspace/data/live/pairs.json`。
4. **官方镜像依赖装在 ftuser(uid 1000) 的 user-site**：以 root 运行必然
   `ModuleNotFoundError: freqtrade`。修复：compose 指定 `user: "1000:1000"`，
   并保证挂载目录对该 uid 可读写（`dshc-init.sh`）。
5. **未来函数**：结算资金费率必须在结算时刻**之后**才生效，序列索引 +1ms 后再 ffill。
6. **CoinDesk RSS 308 重定向**：urllib 不自动跟随 307/308，客户端手动处理 Location。
7. **cryptoslate / Reddit 源不可用**：Cloudflare 403，已从源列表剔除，换成 8 个实测 200 的源。
8. **恐贪指数 301 陷阱**：`api.alternative.me/fng`（无尾斜杠）会 301 到 HTML，
   必须用 `/fng/`。
