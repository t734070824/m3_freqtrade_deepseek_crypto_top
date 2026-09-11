# 变更记录 (北京时间 UTC+8 标注)

## 2026-09-11 23:50 北京时间(15:50 UTC) — v0.3.0 风控数学加固与单测

### ⚠️ 严重问题修复(由独立观测代理发现并复核)

**S1 单笔实际风险是设计值的 4 倍**: `custom_stake_amount` 用「风险预算 / 止损价格距离」
定仓位时**漏乘杠杆**。合约模式下 `stake` 是保证金, 实际敞口 = stake × leverage,
因此止损亏损 = stake × leverage × d —— 实测 164 USDT 保证金 × 4x × 5% 止损 ≈ 33 USDT(权益 3.3%),
而注释声称 0.55%; 6 笔满仓同时止损约 -20% 权益。

**S3 限流错误体被当成数据**: 币安在限流时返回 **HTTP 200 + `{"code":-1003,...}`**,
`BinanceFutures.basis()` 把 dict 包成列表, `job_basis` 硬索引 `d["timestamp"]` 抛 KeyError,
导致整轮 40 个合约的基差数据被丢弃(`basis_snap` 出现 5 分钟桶缺口), 而 HTTP 统计里却记为 `ok`。

### 修复
1. **风控数学抽成纯函数模块** `freqtrade/user_data/stops_core.py`(与框架解耦), 新增 19 条单元测试
   `freqtrade/tests/unit/test_stops_core.py` 钉死两个量纲不变量:
   - `custom_stoploss 返回值 / 杠杆 == 价格距离`(事故: 曾被算成 0.11% 贴价止损)
   - `止损触发时权益回撤 <= 风险预算`(事故: 曾漏乘杠杆放大 4 倍)
   并扫描全部 (止损距离 × 杠杆 × 预算) 组合, 断言权益回撤永不超过 1.5% 硬上限。
2. **仓位决策升级为 `plan_position()`**: 风险预算为**上限**而非等号; 当预算与仓位上限冲突时
   **降低实际风险**而不是超限; 若为满足风险上限必须跌破最小仓位, 则**放弃这笔交易**
   (宁可错过, 不要超风险)。新增 `risk_ceiling=1.5%`、`TARGET_STAKE_RATIO=25%`。
3. **杠杆自适应安全界** `leverage_for_risk()`: 保证「止损距离 × 杠杆」不会把仓位顶到上限。
4. **HTTP 层识别 200+错误体**: `httpx.Client.get` 检测 `{"code": .., "msg": ..}`,
   计入 err 并触发限速器熔断(降速 20% 持续 180 秒)。
5. **采集容错**: `job_basis/job_oi/job_ratio` 改为按 `timestamp` 字段过滤合法行, 单个合约失败
   不再拖垮整轮; `binance.basis()` 遇非列表结构直接抛错。
6. **看板 pairlist 收敛**: `/api/pairlist` 严格输出 `DSHC_TOP_N`(默认 40)个 USDT 计价合约,
   过滤非 ASCII 合约名与稳定币 —— 此前输出全部候选导致 whitelist 膨胀到 55+ 并加剧限流。
7. **启动加速**: 移除 `fiat_display_currency`(启动时会请求 CoinGecko, 实测不可达, 拖慢 2 分 39 秒)。
8. **运维**: 采集器启动即执行一次 prune; 退出前落盘状态; 看门狗 `scripts/dshc-watchdog.sh`
   周期检查容器/心跳/API/候选池新鲜度/429 限流; 测试入口 `scripts/dshc-test.sh`。
9. 兜底 `stoploss` 由 -0.34 收敛到 **-0.20**(4x 下 5% 价格距离), 与风险预算口径一致。

### 验收建议
* 观察期请勿频繁重建容器(前一观测窗口内 freqtrade 被重建 9 次, 不构成稳定性依据)。
* 资金费在 00:00 北京时间(16:00 UTC)结算后复核 `funding_fees`。
* 分批收割需浮盈 >= 10% 才会触发, 抓到一次大浮盈后复核减仓链路。

---

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
