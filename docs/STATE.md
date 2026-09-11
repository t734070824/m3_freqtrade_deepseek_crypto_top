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
