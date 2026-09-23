# 回测对比报告

> 本文件由 `docs/experiment_index.csv` 自动生成，请勿手工编辑。
> `provisional`、`invalid` 和 `retired` 结果仅用于追溯，不得直接作为论文有效结论。

## 关键对比

| 实验 | 状态 | 策略 | 累计收益 | 夏普 | 最大回撤 | 相对 LLM 收益 | 相对 LLM 夏普 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| ppo_weekly_aligned_20260913 | provisional | VADER Top-K | 12.04% | 1.22 | 4.53% | 2.04% | 0.35 |
| ppo_weekly_aligned_20260913 | provisional | PPO Top-K | 11.25% | 0.87 | 5.46% | 1.25% | 0.00 |
| ppo_weekly_aligned_20260913 | provisional | LLM Top-K | 10.00% | 0.87 | 5.90% | 0.00% | 0.00 |
| ppo_weekly_aligned_20260913 | provisional | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |
| ppo_dailyfreq_20260913 | retired | VADER Top-K | 12.04% | 1.22 | 4.53% | 2.04% | 0.35 |
| ppo_dailyfreq_20260913 | retired | PPO Top-K | 8.09% | 0.50 | 6.71% | -1.91% | -0.37 |
| ppo_dailyfreq_20260913 | retired | LLM Top-K | 10.00% | 0.87 | 5.90% | 0.00% | 0.00 |
| ppo_dailyfreq_20260913 | retired | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |
| backtest_20260922_200323 | historical_incomplete | PPO Top-K | 21.59% | 1.24 | 11.76% | 12.09% | 0.45 |
| backtest_20260922_200323 | historical_incomplete | LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| backtest_20260922_200323 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| backtest_20260922_191842 | historical_incomplete | PPO Top-K | 25.72% | 1.27 | 12.71% | 16.22% | 0.48 |
| backtest_20260922_191842 | historical_incomplete | LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| backtest_20260922_191842 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| backtest_20260922_191704 | historical_incomplete | PPO Top-K | 25.44% | 1.26 | 12.71% | 15.94% | 0.47 |
| backtest_20260922_191704 | historical_incomplete | LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| backtest_20260922_191704 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| backtest_20260922_185614 | historical_incomplete | PPO Top-K | 18.32% | 0.91 | 14.73% | 10.37% | 0.33 |
| backtest_20260922_185614 | historical_incomplete | LLM Top-K | 7.95% | 0.58 | 7.45% | 0.00% | 0.00 |
| backtest_20260922_185614 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 24.10% | 1.41 |
| backtest_20260922_184629 | historical_incomplete | PPO Top-K | 13.57% | 0.97 | 5.16% | 10.37% | 1.10 |
| backtest_20260922_184629 | historical_incomplete | LLM Top-K | 3.20% | -0.13 | 5.66% | 0.00% | 0.00 |
| backtest_20260922_184629 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 28.85% | 2.12 |
| baseline_20260902 | historical_incomplete | VADER Top-K | 12.04% | 1.22 | 4.55% | 2.04% | 0.35 |
| baseline_20260902 | historical_incomplete | LLM Top-K | 10.00% | 0.87 | 5.91% | 0.00% | 0.00 |
| baseline_20260902 | historical_incomplete | Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |

## ppo_dailyfreq_20260913

- 时间：unknown；状态：retired；区间：2025-08-11~2026-08-09
- 目的：诊断旧日频 PPO 迁移表现
- 改动：接入旧日频 PPO
- 已知问题：训练环境与真实回测口径不一致
- 证据：`output/backtest_20260913_195527/manifest.json`；代码：`worktree based on 7ff7c02; PPO implementation was uncommitted at run time`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |
| LLM Top-K | 10.00% | 0.87 | 5.90% | 0.00% | 0.00 |
| PPO Top-K | 8.09% | 0.50 | 6.71% | -1.91% | -0.37 |
| VADER Top-K | 12.04% | 1.22 | 4.53% | 2.04% | 0.35 |

## ppo_weekly_aligned_20260913

- 时间：unknown；状态：provisional；区间：2025-08-11~2026-08-09
- 目的：验证周频逆波动率 PPO 的迁移表现
- 改动：日频改为周频并对齐权重
- 已知问题：单种子、单样本外窗口，尚未完成 Gate 验收
- 证据：`output/backtest_20260913_224902/manifest.json`；代码：`worktree based on 7ff7c02; PPO implementation was uncommitted at run time`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |
| LLM Top-K | 10.00% | 0.87 | 5.90% | 0.00% | 0.00 |
| PPO Top-K | 11.25% | 0.87 | 5.46% | 1.25% | 0.00 |
| VADER Top-K | 12.04% | 1.22 | 4.53% | 2.04% | 0.35 |

## backtest_20260922_200323

- 时间：2026-09-22T20:03:23；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：风险对齐 PPO 回测
- 改动：切换至 ppo_stage1_riskaligned_20260922
- 已知问题：运行时未记录具体改动与随机种子
- 证据：`output/backtest_20260922_200323/manifest.json`；代码：`2eb2496c7420fba8037bc4edb67b8f8a69e59109`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| PPO Top-K | 21.59% | 1.24 | 11.76% | 12.09% | 0.45 |

## backtest_20260922_191842

- 时间：2026-09-22T19:18:42；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：P0 PPO CUDA 诊断回测
- 改动：保留含 decisions 诊断的重复运行
- 已知问题：与已删除的 191026 指标重复；运行时未记录随机种子
- 证据：`output/backtest_20260922_191842/manifest.json`；代码：`2eb2496c7420fba8037bc4edb67b8f8a69e59109`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| PPO Top-K | 25.72% | 1.27 | 12.71% | 16.22% | 0.48 |

## backtest_20260922_191704

- 时间：2026-09-22T19:17:04；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：P0 PPO 诊断回测
- 改动：新增调仓级 decisions 诊断
- 已知问题：运行时未记录具体改动与随机种子
- 证据：`output/backtest_20260922_191704/manifest.json`；代码：`2eb2496c7420fba8037bc4edb67b8f8a69e59109`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.55% | 1.20 |
| LLM Top-K | 9.50% | 0.79 | 6.61% | 0.00% | 0.00 |
| PPO Top-K | 25.44% | 1.26 | 12.71% | 15.94% | 0.47 |

## backtest_20260922_185614

- 时间：2026-09-22T18:56:14；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：P0 PPO 回测
- 改动：模型为 ppo_stage1_p0_20260922
- 已知问题：运行时未记录具体改动与随机种子
- 证据：`output/backtest_20260922_185614/manifest.json`；代码：`2eb2496c7420fba8037bc4edb67b8f8a69e59109`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 24.10% | 1.41 |
| LLM Top-K | 7.95% | 0.58 | 7.45% | 0.00% | 0.00 |
| PPO Top-K | 18.32% | 0.91 | 14.73% | 10.37% | 0.33 |

## backtest_20260922_184629

- 时间：2026-09-22T18:46:29；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：P0 PPO 回测
- 改动：模型为 ppo_stage1_p0_20260922
- 已知问题：运行时未记录具体改动与随机种子
- 证据：`output/backtest_20260922_184629/manifest.json`；代码：`2eb2496c7420fba8037bc4edb67b8f8a69e59109`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 28.85% | 2.12 |
| LLM Top-K | 3.20% | -0.13 | 5.66% | 0.00% | 0.00 |
| PPO Top-K | 13.57% | 0.97 | 5.16% | 10.37% | 1.10 |

## baseline_20260902

- 时间：2026-09-02T13:42:50；状态：historical_incomplete；区间：2025-08-11~2026-08-09
- 目的：历史回填：LLM 与 VADER 公式暴露基线
- 改动：历史运行未记录改动说明
- 已知问题：缺少 manifest、数据指纹和运行时 Git 状态
- 证据：`historical_missing_manifest`；代码：`unknown`；工作区脏：historical_unknown

| 策略 | 累计收益 | 夏普 | 最大回撤 | 相对同次基线收益 | 相对同次基线夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Buy & Hold | 32.05% | 1.99 | 10.58% | 22.05% | 1.12 |
| LLM Top-K | 10.00% | 0.87 | 5.91% | 0.00% | 0.00 |
| VADER Top-K | 12.04% | 1.22 | 4.55% | 2.04% | 0.35 |
