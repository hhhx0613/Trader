# 量化交易 LLM 系统

LLM 负责新闻/事件选股；周频 PPO 或波动率目标公式决定总暴露；规则风控只缩减或否决交易。目标是用可复现回测验证模块是否有增量价值，而非预设其有效。

## 当前状态

- LLM Top-K、VADER、Buy & Hold、公式暴露和 PPO Top-K 已接入统一回测入口。
- PPO 训练/部署已对齐周频、逆波动率、真实成本、T+1 开盘成交、R1/R1a 单标的止损和 R2 当日禁买。
- PPO 尚未通过 Go/No-Go：仍缺多种子、滚动样本外和 block bootstrap。
- LLM alpha 尚未获证明；现有 alpha 审计的 LLM 结果为空，不能得出 LLM 无效的结论。
- 权威架构见 [`docs/Plan_v2.md`](docs/Plan_v2.md)，待办见 [`docs/待修改清单.md`](docs/待修改清单.md)。

## 架构

```text
PIT 新闻/财报 → LLM 个股事件评分 → Top-K 选股
                                      ↓
市场数值特征 + 账户状态 → PPO 或波动率目标公式 → 总暴露
                                      ↓
                         逆波动率权重 → R1/R1a/R2 风控 → T+1 开盘执行
```

`LLM Top-K` 与 `PPO Top-K` 使用相同选股和个股权重，只替换总暴露，形成 PPO 的干净 A/B。

## 环境

Python 3.10+：

```bash
conda create -n trader python=3.10 -y
conda activate trader
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，按需配置 `DEEPSEEK_API_KEY`、`ALPHA_VANTAGE_API_KEY`、`FINNHUB_API_KEY` 和 `GLM_API_KEY`。不要提交 `.env`、缓存或密钥。

## 运行回测

每次回测必须记录研究语境：

```bash
python scripts/run_backtest.py \
  --purpose "验证周频 PPO 相对公式暴露的增量" \
  --change-note "仅替换 PPO 模型，其他口径不变" \
  --known-limitations "单随机种子，尚未完成滚动样本外验证" \
  --control-experiment ppo_weekly_aligned_20260913 \
  --seed 42
```

- `--pool`、`--start`、`--end`：候选池与回测区间。
- `--ppo-model`、`--ppo-device cpu|cuda`：模型和推理设备。
- `--skip-vader`：跳过 VADER 对照。
- `--baseline-strategy "LLM Top-K"`：计算同次相对差值的基线。
- `--status provisional|valid|invalid|retired`：未经审阅时使用 `provisional`。

默认是 10 股候选池、Top-K=5、ISO 周调仓。PPO 模型不存在时会明确跳过 PPO 路径。

## 输出与论文溯源

每次运行建立不可覆盖的 `output/backtest_<run_id>/`：

- `summary`：策略绩效；`equity`：净值曲线；`trades`：交易明细。
- `decisions`：目标/实际暴露、回撤与 R2 状态（如有）。
- `report`：单次摘要；`manifest.json`：参数、Git 状态、数据指纹、模型哈希、目的、改动和局限性。

同时自动更新：

- [`docs/experiment_index.csv`](docs/experiment_index.csv)：唯一结构化登记册；一行是“实验 × 策略”。前列为状态、目的、收益、夏普、回撤、相对 LLM 差值、改动和问题。
- [`docs/backtest_report.md`](docs/backtest_report.md)：自动生成的阅读版对比报告，先给关键指标，后给溯源细节。

原始 CSV 留在本地 `output/` 作为证据；Git 版本化 `manifest.json`、登记册和总报告。缺元数据的历史运行标为 `historical_incomplete`，不能作为论文有效结论。

## 当前结果如何解读

- 旧日频 PPO 为 `retired`：训练环境和真实回测口径不一致。
- 周频对齐 PPO 为 `provisional`：相对公式 LLM 基线累计收益约 +1.25 个百分点、夏普持平，不能宣称优势。
- 2026-09-22 PPO 调试运行均为 `historical_incomplete`，仅供诊断。
- Buy & Hold 的风险暴露不同，不能直接用绝对收益推导选股 alpha。

详细结果见 [`docs/backtest_report.md`](docs/backtest_report.md)。

## 项目结构

```text
agents/                     LLM、VADER、Top-K 决策
core/                       数据、指标、回测、风控、PPO
scripts/run_backtest.py     统一回测入口
scripts/train_ppo.py        PPO 训练入口
models/                     PPO 模型归档
docs/Plan_v2.md             权威架构与决策
docs/experiment_index.csv   自动维护的实验登记册
docs/backtest_report.md     自动生成的对比报告
output/                     原始回测证据
data/cache/                 本地缓存（不提交 Git）
```

## 后续优先级

1. 修复并重做 LLM alpha 审计。
2. 完成 PPO 多种子、滚动样本外与 bootstrap Go/No-Go 验收。
3. 补齐模型训练元数据与关键确定性测试。
4. Gate 结论明确后，清理或归档 Stage 2、多 Agent、IBKR 等非主路径内容。
