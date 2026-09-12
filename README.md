# M3-DSH DeepSeek Crypto Top — 币安合约涨幅榜趋势交易系统

> 基于开源 [freqtrade](https://www.freqtrade.io/) 的 `Binance USD-M 永续合约` **涨幅榜** 趋势跟随交易系统。
> 多空双向、主趋势内持续参与/持有/收割，**资金费率是第一约束**，场内外多源数据融合打分选币。
> 不做历史回测（标的池是动态涨幅榜，回测存在严重选择偏差），一律通过 **dry-run 快速迭代**。

容器名统一前缀：**`m3dsc`**（本项目唯一标记）。

---

## 1. 系统架构

```
                    ┌──────────────────────────── Binance USD-M (公共接口, 无需 API Key) ──────────┐
                    │ ticker/24hr · premiumIndex(资金费率) · openInterest(+Hist) · fundingRate      │
                    │ topLongShortAccount/PositionRatio · globalLongShortAccountRatio · takerRatio │
                    │ ticker/bookTicker(盘口) · basis(基差) · exchangeInfo                          │
                    └───────────────────────────────────┬───────────────────────────────────────────┘
                                                        │
   ┌── 场外免费数据 ──┐                                  ▼
   │ alternative.me   │        ┌──────────────────────────────────────────────┐
   │  恐贪指数         ├───────►│  m3dsc-market-collector   (多线程并发采集)     │
   │ 8 家加密媒体 RSS  │        │  ticker 60s / mark 60s / book 120s            │
   └──────────────────┘        │  oi 180s / ratio 300s / basis 300s / news 600s│
                               └───────┬───────────────────────────┬──────────┘
                                       │                           │
                      data/m3dsc_market.db (SQLite WAL)      data/live/*.json
                                       │                           │
                       ┌───────────────▼──────────────┐   ┌────────▼───────────────┐
                       │  打分引擎 dshc/screener.py     │   │ pairs.json / watchlist │
                       │  score ∈ [-100, +100]        │   │  .json (原子替换)        │
                       └───────────────┬──────────────┘   └────────┬───────────────┘
                                       │                           │ RemotePairList(file:///)
                                       ▼                           ▼
                       ┌──────────────────────────────────────────────────────────┐
                       │  m3dsc-freqtrade-dryrun   (freqtrade 2026.8 + M3GainersTrend) │
                       │  5m/1h/4h 三周期共振 · ATR 跟踪 · 分批收割 · 费率门控       │
                       └───────────────────────────┬──────────────────────────────┘
                                                   │ REST API :18081
                                       ┌───────────▼──────────────┐
                                       │  m3dsc-dashboard  :18082 │  自研看板
                                       └──────────────────────────┘
```

### 容器一览

| 容器名 | 作用 | 端口 |
|---|---|---|
| `m3dsc-market-collector` | 币安合约 + 场外数据采集，产出时序库与候选池 | — |
| `m3dsc-freqtrade-dip` | **对照实验 B**：急跌反弹策略（M3DipRevert） | 127.0.0.1:18084 |
| `m3dsc-freqtrade-dryrun` | **实验 A**：动量延续策略（M3GainersTrend），动态涨幅榜选币 | 127.0.0.1:18081 |
| `m3dsc-dashboard` | 监控看板 + 动态候选池 API（RemotePairList 数据源） | 127.0.0.1:18083 |

> **A/B 对照实验**：两个 dry-run 共用同一份数据与同一套风控内核（`stops_core`），
> 只有入场逻辑不同，独立记账，用 `python3 scripts/dshc_compare.py` 并排对比。
> 胜负判据只有一个：**期望值/笔 > 0 且盈亏比 > 1**。

---

## 2. 快速开始

```bash
cd /home/dsh_3081/m3_freqtrade_deepseek_crypto_top

bash scripts/dshc-init.sh          # 生成 .env(随机密钥) + 目录权限
bash scripts/dshc-up.sh --build    # 构建镜像并启动全部服务
bash scripts/dshc-status.sh        # 总览: 容器/账户/持仓/候选池/采集健康
bash scripts/dshc-verify.sh        # 端到端自检
bash scripts/dshc-logs.sh collector 100
bash scripts/dshc-down.sh          # 停止
```

| 地址 | 说明 |
|---|---|
| http://127.0.0.1:18083/ | 自研看板（60 秒自动刷新） |
| http://127.0.0.1:18081/ | freqtrade REST API（用户名/密码见 `.env`） |
| http://127.0.0.1:18083/api/summary | 机器可读摘要 |
| http://127.0.0.1:18083/api/pairlist | 动态候选池（freqtrade RemotePairList 数据源） |

> 端口说明：`18082` 已被同主机上另一个项目占用，本项目看板使用 **18083**。

---

## 3. 时间约定（**重要**）

* **存储**：所有落库时间一律为 **UTC 毫秒时间戳**（`ts_ms`），无歧义、可直接跨源对齐。
* **展示**：任何对用户可见的时间都必须显式标注时区，例如
  `2026-09-11 22:18:38 北京时间(UTC+8)` 与 `2026-09-11 14:18:38 UTC` 成对出现。
* 币安返回的 `fundingTime` / `nextFundingTime` / `timestamp` 同样是 **UTC 毫秒**。
* 容器内 `TZ=Asia/Shanghai`（便于本地阅读日志），但**不改变**上述存储约定。
* 代码中的换算工具统一在 `dshc/timeutil.py`（`fmt()` UTC / `fmt_cn()` 北京时间 / `fmt_both()` 双标注）。

---

## 4. 数据源（全部免费、无需 API Key）

### 4.1 交易所场内（Binance USD-M）

| 数据 | 接口 | 频率 | 用途 |
|---|---|---|---|
| 24h 行情 | `/fapi/v1/ticker/24hr` | 60s | **涨幅榜**排序、成交额流动性门槛 |
| 标记价+资金费率 | `/fapi/v1/premiumIndex` | 60s | **持仓成本/补贴**、下次结算时间 |
| 结算费率历史 | `/fapi/v1/fundingRate` | 1h | 费率历史序列（严格按结算时刻对齐，防未来函数） |
| 持仓量 | `/fapi/v1/openInterest` | 180s | OI 快照 |
| 持仓量历史 | `/futures/data/openInterestHist` | 300s | OI 变化率（资金真实进出） |
| 大户账户多空比 | `/futures/data/topLongShortAccountRatio` | 300s | 拥挤度 |
| 大户持仓多空比 | `/futures/data/topLongShortPositionRatio` | 300s | 拥挤度（更重要） |
| 全局账户多空比 | `/futures/data/globalLongShortAccountRatio` | 300s | 散户情绪（反向指标） |
| 主动买卖比 | `/futures/data/takerlongshortRatio` | 300s | 短期资金流向 |
| 盘口 | `/fapi/v1/ticker/bookTicker` | 120s | 价差/滑点评估 |
| 基差 | `/futures/data/basis` | 300s | 期货升贴水（多头拥挤） |

* 限速：`REQUEST_WEIGHT 2400/min/IP`；`/futures/data/*` 家族 weight=0 但另限 1000 req/5min。
* 采集器对**全市场**请求做了合并（ticker 一次 40 weight 拿全部 526 个合约）。

### 4.2 场外（宏观/情绪）

| 数据 | 来源 | 频率 |
|---|---|---|
| 恐贪指数 | `https://api.alternative.me/fng/`（**必须带尾斜杠**，否则 301 到 HTML） | 900s |
| 新闻情绪 | 8 家免费 RSS（Cointelegraph / CoinDesk / Decrypt / The Block / Bitcoin Magazine / NewsBTC / crypto.news / AMBCrypto） | 600s |

> 已实测**不可用**并剔除：cryptoslate RSS（Cloudflare 拦截）、Reddit JSON（403）。
> 本机 SOCKS 代理 `127.0.0.1:10808` 目前不可用，系统走**直连**（香港出口，无地域限制）。

---

## 5. 选币打分引擎（`dshc/screener.py`）

对每个候选合约输出 `score ∈ [-100, +100]`：**> 0 表示做多有利，< 0 表示做空有利**。

| 因子 | 权重 | 逻辑 |
|---|---|---|
| `mom` | 30 | 24h 涨幅 tanh 压缩；**涨幅 > 25% 视为过热，收益递减并转负**（拒绝追高） |
| `mom_mid` | 18 | OI 1h 变化 + taker 主动买卖比方向 |
| `funding` | 22 | **年化费率**：>40% 扣分（做多成本高）、>110% 直接判负；< -10% 加补贴分 |
| `oi` | 14 | 持仓量与价格同向放大 = 真趋势；反向 = 可疑 |
| `ls` | 10 | 大户多空比越极端越反向扣分（拥挤度） |
| `taker` | 8 | 主动买卖比（>1 买方主动） |
| `basis` | 6 | 期货大幅升水 = 多头拥挤 |
| `liq` | 6 | 盘口价差过大扣分 |
| `fng` | 4 | 恐贪极端值抑制同向追单 |

候选池 = 涨幅榜前 40 + 跌幅榜前 10（做空侧）+ 主流锚点，按 `|score|` 排序输出。

---

## 6. 交易策略（`freqtrade/user_data/strategies/M3GainersTrend.py`）

### 6.1 三周期共振

| 周期 | 判定 |
|---|---|
| **4h 主趋势** | `EMA50 vs EMA200` + 价格位置 → `trend_dir ∈ {+1, 0, -1}`，只有 `±1` 才允许开仓 |
| **1h 相位** | `close/EMA21/EMA50` 多头或空头排列 → `phase ∈ {+1, 0, -1}`；ADX≥15；延伸度 < 12% |
| **5m 触发** | 突破 20 根新高新低 / 回踩均线确认 / MACD 柱反转，三者之一 |

### 6.2 资金费率的四处强制约束（**核心**）

1. **开仓门控**：做多要求 `年化费率 ≤ +45%`；做空要求 `≥ -50%`。
   *实时费率* 通过 `self.dp.funding_rate(pair)` 获取（ccxt premiumIndex），与已结算历史双重校验。
2. **仓位缩放**：年化 > +20% 时仓位 ×0.8；年化 < -10%（做多有补贴）时 ×1.1。
3. **持仓期强制退出**：持多期间年化 > +90% → `funding_exit_long`；持空期间年化 < -90% → `funding_exit_short`。
4. **评分前置**：`funding` 因子权重 22，直接决定该标的是进多头池还是空头池。

### 6.3 持续持有 + 持续收割

* **初始止损**：`2.6 × ATR(5m)`，钳制在 2%~12% 价格距离。
* **跟踪止盈**：浮盈 > 2.5% 启动，距离 `2.2 × ATR`；同时锁定「已有利润 - 0.8%」。
* **阶梯收紧**：浮盈 > 25% 收紧到 6%；> 60% 收紧到 5%。
* **分批收割**：浮盈 +10% 减 35%，+30% 再减 35%（`adjust_trade_position`），主仓继续跟随趋势。
* **硬止损**：收益率 ≤ -8.5%（权益口径）立即市价离场。
* **动量衰竭离场**：5m RSI 极端 + MACD 反向且已有浮盈 → 收割。
* **僵尸仓位**：持有 > 72h 且 |收益| < 0.4% → 释放资金；最长持有 10 天。

### 6.4 仓位与杠杆

* 杠杆基准 **4x**（上限 5x），按 ATR 波动率与 ADX 自适应增减。
* 仓位由**风险预算**决定：目标单笔止损触发时损失约 0.6% 权益
  `stake = 风险预算 / (价格止损距离 × 杠杆)`，并限制在权益的 3%~30%。
* 组合最多 6 笔并发，`tradable_balance_ratio = 0.95`。

---

## 7. 动态选币接入方式（重要技术决策）

freqtrade 2026.x 中 `pairlists[].method` 是**枚举白名单**校验的，用户**无法**注册自定义 `IPairList`
（`PairListResolver` 只搜索内置 `freqtrade/plugins/pairlist/`）。官方留给用户的扩展点是 **`RemotePairList`**，
它支持 `file:///` 与 HTTP，契约格式：

```json
{ "pairs": ["BTC/USDT:USDT", "..."], "refresh_period": 60 }
```

因此本系统由采集器每分钟把打分池写入 `data/live/pairs.json`，freqtrade 通过
`file:///workspace/data/live/pairs.json` 读取（`keep_pairlist_on_failure: true` 保证失败时沿用上一份）。
链路：`RemotePairList → SpreadFilter(≤0.8%) → AgeFilter(≥2天) → PriceFilter`。

---

## 8. 目录结构

```
├── docker-compose.yml           # 容器编排 (前缀 m3dsc)
├── .env.example                 # 配置模板 (.env 由 dshc-init.sh 生成, 不入库)
├── dshc/                        # 自研 Python 包
│   ├── binance.py               # 币安 USD-M 公共数据客户端
│   ├── collector.py             # 多线程采集守护进程
│   ├── screener.py              # 涨幅榜打分引擎
│   ├── dashboard.py             # 监控看板 (Flask)
│   ├── db.py / httpx.py / timeutil.py / config.py
├── config/config-dryrun.json    # freqtrade dry-run 配置
├── freqtrade/user_data/strategies/M3GainersTrend.py   # 交易策略
├── docker/Dockerfile.{freqtrade,collector}
├── scripts/dshc-*.sh            # 运维脚本
├── data/                        # SQLite 时序库 + live/*.json (运行时生成)
├── logs/                        # 日志
└── docs/                        # 设计文档
```

---

## 9. 风险与注意事项

* **仅以 dry-run 评估**：涨幅榜标的池动态变化，历史上从未出现在榜单里的币不会进入回测，
  因此本项目**不做回测**，一切以 dry-run 实盘数据为准。
* **资金费率是负期望来源**：多头拥挤的币种年化费率常达 +50%~+200%，
  在第 6.2 节的四处约束生效前，不要放宽 `FUNDING_MAX_LONG_ANN`。
* **小市值合约滑点**：价差 > 25bps 的标的已由 `SpreadFilter`/打分器过滤；
  实际转实盘前需用真实盘口复检。
* **实盘开关**：`.env` 中的 `DSHC_BINANCE_KEY/SECRET` 留空即保持 dry-run。
  转实盘需同时修改 `config/config-dryrun.json` 的 `dry_run` 并单独评估，**不要直接改 dry-run 配置**。
* **同 IP 限速**：服务器上还有其它 freqtrade 实例共用 `2400 weight/min`，采集器已做全市场合并与速率上限。

---

## 10. 变更记录

见 `docs/CHANGELOG.md`。

---

## 11. A/B 对照实验与判据

| 实验 | 容器 | 策略 | 假设 | 端口 |
|---|---|---|---|---|
| A | `m3dsc-freqtrade-dryrun` | M3GainersTrend | 动量在**延续区**(24h>=20%)继续 | 18081 |
| B | `m3dsc-freqtrade-dip` | M3DipRevert | 急跌(15m<=-3.5%)后反弹 | 18084 |

两点设计保证可比: ① 共用同一个采集器的数据与同一份候选池;
② 共用同一套风控内核 `stops_core`(风险预算/距离钳制/风险上限), 只有入场逻辑不同。

**唯一判据**: 期望值/笔 > 0 且 盈亏比 > 1。样本少于 20 笔时不下结论。

```bash
python3 scripts/dshc_compare.py --list   # 并排对比 A/B
python3 scripts/dshc_trades.py           # A 的交易归因
python3 scripts/dshc_excursion.py        # 止损是否太紧(MFE/MAE)
python3 scripts/dshc_momentum.py         # 动量/跌幅的条件收益
```
