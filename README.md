# 可审计量化研究与模拟执行系统

当前仓库保留 LLM、VADER 和 PPO 的统一回测基线；项目将逐步演进为从 PIT 数据研究到模拟执行的可审计多 Agent 应用。目标不是直接追求收益最大化，而是让每一项决策都能追溯其可得证据、研究论点、风险审查、成本约束和最终结果。

权威开发计划见 [docs/Plan.md](docs/Plan.md)；毕设研究叙事见 [docs/agent_application_proposal.md](docs/agent_application_proposal.md)；历史实验以 [docs/experiment_index.csv](docs/experiment_index.csv) 为唯一结构化登记册。

## 当前基线

- 统一回测入口包含 LLM Top-K、VADER、Buy & Hold、公式总暴露与 PPO Top-K。
- LLM/VADER 只使用决策日可得的 PIT 新闻；交易按 T+1 开盘执行。
- 个股权重使用逆波动率；PPO 或波动率目标公式控制组合总暴露；R1/R1a/R2 规则负责硬风控。
- PPO 尚未通过多随机种子、滚动样本外和 block bootstrap 的 Go/No-Go 验证。
- LLM alpha 尚未被充分证明；现有结果只能作为待复核基线，不能视为论文结论。

## 目标架构

```text
PIT 数据 → EvidenceCard → 个股研究 Agent → ClaimCard
        → 全局组合委员会 → PortfolioPolicy → 风控与成本门控
        → 模拟执行 → OutcomeCard 与校准记忆
```

原始证据、LLM 论点和交易动作将严格分层。最终系统不固定 Top-K：委员会可在全股票池和当前持仓上下文中选择买入、持有、减仓、卖出或观望；确定性组合政策、成本模型和风险引擎将意图转换为可执行的股票加现金权重。

## 环境

Python 3.10+：

```bash
conda create -n trader python=3.10 -y
conda activate trader
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，按数据源和模型需要配置 `DEEPSEEK_API_KEY`、`ALPHA_VANTAGE_API_KEY`、`FINNHUB_API_KEY`、`GLM_API_KEY` 和 `SEC_USER_AGENT`。`SEC_USER_AGENT` 必须是含联系邮箱的应用标识，例如 `TraderResearch/0.1 name@example.com`。不要提交 `.env`、缓存或密钥。

## 测试缓存

pytest 的跨会话缓存已禁用，避免当前 Windows 环境遗留 `.pytest_cache` 或 `pytest-cache-files-*` 临时目录；每次运行的 fixture 固定写入并覆盖 `tmp/pytest/`，该目录已由 Git 忽略。这不影响测试运行。

阶段一研究数据层的离线回归测试：

```bash
python -m pytest tests/research -q
```

它验证阶段 0 冻结契约的账本往返与确定拒绝（引用约束、路由分支、原因码、权重字段禁区），以及 PIT 拒绝、转载去重、建卡/冻结双入口与 `select_evidence` 池选卡、内容哈希定位、DataGateway 查询审计与新闻采集的段粒度（历史整段网格省配额，未走完的段拆为已收口的单日段）。该路径不导入 LangGraph、cvxpy 或 PPO 依赖。

## 研究数据采集（阶段 1 主线）

```bash
python scripts/capture_research_snapshot.py --pool NVDA   # 单票完整轮：采集 -> 建卡 -> 冻结
python scripts/capture_research_snapshot.py --no-snapshot # 每日节奏：只建卡入池，不冻结
python scripts/capture_research_snapshot.py --as-of 2026-10-01T18:00:00+00:00  # 历史时点回放
```

- 实盘模式（不传 `--as-of`）在全部采集完成后才定决策时点，本轮到达的数据本轮即可用；回放模式时点钉死，本轮新拉数据被 PIT 闸门拒绝。
- 产出全部落 `data/research/`：正文分库（news/market/filings.db，内容寻址 append-only）与账本 ledger.db（卡片/快照/trace）；`data/cache/` 下另有可弃的网络备忘层（如 SEC 应答 JSON），不是事实存储。
- 配额与节奏：Alpha Vantage 免费档 25 次/天、段间 15s 节流；coverage 台账记住“问过哪段”不重烧。Windows 下建议直调环境内 `python.exe`（`conda run` 会吞输出）。

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

当前基线默认使用 10 只候选股票、Top-K=5 和 ISO 周调仓；这不是目标 Agent 系统的持股数量限制。

## 输出与实验治理

每次运行建立不可覆盖的 `output/backtest_<run_id>/`：

- `summary`、`equity`、`trades`、`decisions`：策略原始证据。
- `report`：该次运行的人工审阅摘要。
- `manifest.json`：参数、Git 状态、数据指纹、模型哈希、目的、改动和已知局限。

回测同时追加 [docs/experiment_index.csv](docs/experiment_index.csv)。`provisional`、`invalid`、`retired` 或 `historical_incomplete` 记录只用于追溯，不能直接作为论文有效结论。项目不再维护会与登记册漂移的全局 Markdown 回测报告。

## 项目结构

```text
agents/                         LLM、VADER 与当前 Top-K 决策
core/                           数据、指标、回测、风控与 PPO
core/research/                  PIT Snapshot、原始载荷、审计账本与只读 DataGateway
tests/research/                 阶段 0/1 契约、离线数据与审计回归测试
scripts/run_backtest.py         统一回测入口
scripts/capture_research_snapshot.py  研究数据采集/建卡/冻结入口
scripts/train_ppo.py            PPO 训练入口
models/                         PPO 模型归档
docs/Plan.md                    唯一权威开发计划
docs/agent_application_proposal.md  毕设研究提案
docs/experiment_index.csv       实验结构化登记册
output/                         单次回测原始证据
data/cache/                     本地缓存（不提交 Git）
data/research/                  三类研究数据的原始载荷和审计账本（不提交 Git）
```

后续工作顺序以 [docs/development_plan.md](docs/development_plan.md) 的阶段任务为准。
