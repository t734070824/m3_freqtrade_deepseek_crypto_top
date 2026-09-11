# 当前系统状态与运行笔记

> 所有时间均为 **北京时间(UTC+8)**，括号内为 UTC。

## 1. 部署状态

| 项目 | 状态 |
|---|---|
| 容器 | `m3dsc-market-collector` / `m3dsc-freqtrade-dryrun` / `m3dsc-dashboard` 均 running |
| 交易模式 | freqtrade **dry-run**（模拟盘，无需 API Key） |
| 初始资金 | 1000 USDT，杠杆基准 4x（上限 5x），最多 6 笔并发 |
| 策略 | `M3GainersTrend`（三周期共振 + 资金费率四处约束 + ATR 跟踪 + 阶梯收割） |
| 行情采集 | 9 路并发（ticker/mark/book/oi/oi_hist/ratio/basis/fng/news），SQLite WAL |
| 看板 | http://127.0.0.1:18083/ （freqtrade REST 在 127.0.0.1:18081） |
| 代码仓库 | https://github.com/t734070824/m3_freqtrade_deepseek_crypto_top （main 分支已推送） |

## 2. 首次实盘（模拟）验证结果

| 时间 | 事件 |
|---|---|
| 2026-09-11 22:41:58 北京时间 (14:41:58 UTC) | 开仓 `MET/USDT:USDT` 多单 @0.2623，杠杆 4x，仓位 164 USDT |
| 2026-09-11 23:00:35 北京时间 (15:00:35 UTC) | 跟踪止盈平仓 @0.2643，**收益率 +2.77%（+4.55 USDT）** |
| — | 跟踪止损锁定峰值收益约 **74%**，机制符合「持续持有 + 持续收割」设计 |

## 3. 首轮数据分析（样本很小，仅作方向）

数据窗口：2026-09-11 22:45-23:10 北京时间，n=55，前瞻 15 分钟。

| 检验 | 结果 | 结论 |
|---|---|---|
| 打分分桶平均收益 | -0.11% -> +0.13% -> +0.32% -> +0.47% -> +0.93% | **单调递增**，打分有效 |
| Pearson(score, 未来收益) | +0.381 | 正相关 |
| Pearson(OI 1h 变化, 未来收益) | **+0.502** | 最强单因子 -> 权重 14->24 |
| Pearson(年化费率, 未来收益) | +0.194 | 与「高费率=压制」的直觉相反，待更多样本 |
| 涨幅榜 TOP10 vs 全体市场 | **+1.50%（胜率 80%）** vs +0.35%（60%） | 「只做涨幅榜」前提成立 |

## 4. 入场漏斗（2026-09-11 23:35 北京时间，池内样本 n=197）

各门槛通过率：

- `liq=100%` `rend_same=22%` `adx=95%` `score12=57%`
- `funding_ok=93%` `ma_align=100%` `macd_dir=100%` `rsi5_range=99%`
- `rsi4_extreme=98%` `ext=94%` `oi=98%` `vol4=100%` `trigger=7%`

**结论**：主要瓶颈是 (1) 4h/1h 同向（22%）(2) 打分绝对值 >=12（约 57%）(3) 触发条件（7%，已从 2-4% 优化到 7-8%）。

## 5. 已知问题与后续工作

| 优先级 | 事项 | 说明 |
|---|---|---|
| 高 | 继续观察信号数量 | 当前约 1-2 笔/小时；若长期达不到 6 笔并发，可放宽 `trend_same`（如允许 4h 中性 + 1h 强趋势） |
| 高 | 用更多样本复检打分权重 | 数据满 12-24 小时后重跑 `scripts/dshc_analyze.py alpha` |
| 中 | 空头通道验证 | 当前为上涨行情，空头信号尚未被市场验证 |
| 中 | 资金费归因 | 观察 `funding_fees` 是否在持仓跨结算周期后正确累积 |
| 中 | 分批收割验证 | `adjust_trade_position` 需浮盈 >=10% 才触发，尚未触发过 |
| 低 | 榜单名次稳定性分析 | 需要 >=1 天的 `rank_snap` 数据 |

## 6. 常用命令

```bash
bash scripts/dshc-status.sh                    # 总览
bash scripts/dshc-verify.sh                    # 端到端自检
python3 scripts/dshc_analyze.py alpha --horizons 15 60 240
python3 scripts/dshc_analyze.py stability --top_n 10
python3 scripts/dshc_analyze.py bench --horizon 60
docker logs -f m3dsc-freqtrade-dryrun | grep M3
```

## 7. 风险提示

* 币安「每 IP 2400 weight/min」由本机多个项目共享。**不要**把采集器 `rps` 或策略里的
  实时费率查询频率调高 —— 2026-09-11 已因 660 请求/分钟触发过 429 限流事故。
* dry-run 的成交价按盘口模拟，与实际滑点存在偏差；转实盘前必须用真实盘口复检。
* 涨幅榜标的的流动性会随榜单变化急剧改变，`SpreadFilter` 与打分里的 `liq` 因子是必要保护。
## 8. 2026-09-11 23:50 北京时间(15:50 UTC) v0.3.0 更新要点

### 已修复的严重缺陷
| 编号 | 问题 | 影响 | 修复位置 |
|---|---|---|---|
| S1 | 仓位公式漏乘杠杆 | 单笔实际风险为设计值 4 倍 | BTstops_core.plan_positionBT + 单测 |
| S2 | 止损量纲混用 | 浮盈 3.65% 时止损被贴到 0.11%，被噪声扫出 | BTstops_core.stop_price_distanceBT + 单测 |
| S3 | 限流错误体当数据 | 基差整轮丢弃、数据断档且统计为 ok | BThttpx.Client.getBT 识别 200+错误体 |
| A5 | 候选池未截断 | whitelist 膨胀到 55+，加剧限流 | 看板 BT/api/pairlistBT 严格 40 条 |
| A7 | 启动请求 CoinGecko | 启动被阻塞 2 分 39 秒 | 移除 BTfiat_display_currencyBT |

### 新增能力
* **单元测试**：BTfreqtrade/tests/unit/test_stops_core.pyBT（19 条），钉死两个量纲不变量：
  BT|custom_stoploss 返回值| / leverage == 价格距离BT、
  BT止损触发时权益回撤 <= 风险预算BT；并全参数扫描断言权益回撤 <= 1.5% 硬上限。
  运行：BTbash scripts/dshc-test.shBT
* **「多头极端拥挤」反向做空路径**（BTm3_short_revBT）：涨幅榜特有的收割形态 ——
  打分 <= -35 且年化费率 >= 80% 且 4h RSI >= 82 且 5m 动能转弱时逆势做空，
  同时**收取**资金费率；离场由费率回落/RSI 修复接管，不依赖趋势反转。
* **看门狗**：BTbash scripts/dshc-watchdog.sh 300BT，周期检查容器/心跳/API/候选池/429。
* **研究工具**：BTpython3 scripts/dshc_analyze.py alphaBT 等 5 个子命令。

### 仍待验证（下一阶段）
1. 资金费计入：需持仓跨过 00:00 北京时间（16:00 UTC）结算点后看 BTfunding_feesBT。
2. 分批收割：需浮盈 >= 10%，尚未触发过。
3. 空头通道：需市场出现下跌或极端过热行情。
4. 打分权重再校准：数据满 12-24 小时后重跑 BTalphaBT（当前仅 20 分钟样本）。
* 观测期请勿频繁重建容器：22:47-23:35 窗口内 freqtrade 被重建 9 次（含一次 2 分 39 秒停机），
  那次窗口不构成稳定性依据。
