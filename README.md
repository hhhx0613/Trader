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

### 阶段 2 研究图谱

`agents/research/` 提供按标的隔离的 LangGraph，拓扑为 `(Event -> Fundamental) || Market -> Risk Critic -> ResearchPacket`：Event 先确立或弃权论点，再分叉两路——Fundamental 用独立财报证据验证 Event 已确立的论点；Market 是独立分支，Event 弃权时不会被硬风控闸掉（例如硬风险或纯价格触发场景）。`event_agent.py`、`fundamental_agent.py`、`market_agent.py`、`risk_critic.py` 各自承担对应研究职责；`claim_agent.py` 只是它们共用的带引用 ClaimCard 校验边界。

研究节点采用「注入 hook + 单工具补查」：每个 Agent 在调用模型前先执行注入 hook，把自己那一份冻结批次（Event 读 news、Fundamental 读 filing、Market 读 market/peer/regime，并把 Event 论点作只读语境注入以供判断价量确认/背离）无条件拼进初始 prompt，因此模型默认单轮即可产出 ClaimCard，不再花一轮工具去「要」本就该看到的数据；模型唯一可自主发起的只读工具是 `search_evidence`（同维度定向补查：`ResearchTools` 按节点锁定证据类别，Event 只能搜 news、Fundamental 只能搜 filing、Market 只能搜 market，维度分工不给补查开后门），`tools.py`（`ResearchTools`）一遍实现其预算/校验/截断/trace 记账，Agent 文件只声明挂载哪些工具与本节点可见的证据类别。Risk Critic 是例外：仓位/换手成本/regime/peer 四项固定上下文全部注入、不挂载任何工具，以单轮 `chat_json` 输出 verdict。证据仍不足时模型走一次性 `data_request` 弃权而非无限补查。

图谱只通过 `core/research/` 的 `DataGateway` 读取 Snapshot 绑定数据，不生成权重、订单或回测，也绝不触网：`DataGateway` 只读账本与原始载荷库。采集与冻结发生在 Snapshot 之前，统一由 `agents/research/flow.py` 编排（见「运行入口」）。离线验收套件：`python -m pytest tests/research -q`。

`DataGateway` 不暴露任何采集面：没有 `ensure_*` 工具、没有运行生命周期，卡片查询严格限定在所指名 Snapshot 冻结时选定的卡片集合内。冻结窗口确实证据过薄时，节点显式返回 `data_request` 路由并弃权；缺口由下一次采集补齐，未补齐的数据绝不进入本轮决策。`DataGateway.get_news_batch()` 确定性地按情绪分绝对值降序取至多 100 张冻结卡片、至多 24,000 字符（两者任一命中即截断），防止历史大卡片池变成无界的付费模型请求。因为实盘与回放研究读的都是同一份冻结 Snapshot，一个 Snapshot 永远对应一套固定输入集；`ResearchLedger.list_evidence` 在一次查询内完成冻结集合与 symbol 过滤解析，Snapshot 引用的卡片若已不在账本会直接报错。

### 委员会边界

`agents/research/committee.py` 把同一 Snapshot 的各 `ResearchPacket` 汇总为持久化的 `ThesisBook` 与 `PortfolioIntent`。它只接收 packet 摘要，不接触原始证据与 Agent 对话，必须为每只股票输出带引用的意图，且不能推翻 Risk Critic 的 `abstain`/`human_review` 结论；不负责权重、预测、硬风控结果、成本或订单计算。违规拦截分级生效：单只标的越界（bypass 否决、引用出池）只把该标的矫正为 abstain 并保留其余合规意图；响应结构不齐等全局违规则维持全池弃权；任何矫正都会把模型原始响应记入账本，保证“模型想说什么”与“系统采纳了什么”均可重放。

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

它验证阶段 0 冻结契约的账本往返与确定拒绝（引用约束、路由分支、原因码、权重字段禁区），以及 PIT 拒绝、转载去重、建卡/冻结双入口与 `select_evidence` 池选卡、内容哈希定位、DataGateway 查询审计与新闻采集的段粒度（历史整段网格省配额，未走完的段拆为已收口的单日段）；另覆盖统一编排入口 `flow` 的模式门控（daily 不触模型不冻结、decide 缺模型或缺真实账户即拒绝、池选卡冻结一路跑到 PortfolioIntent、同时点重跑得到可读拒绝）。该路径不导入 cvxpy 或 PPO 依赖。

## 运行入口：研究链主线（阶段 1-3）

编排本体在 `agents/research/flow.py` 的 `run_research_round(FlowOptions, model=...)`：阶段门控与 trace_id 生成只在这一处。`scripts/research_run.py` 只暴露「跑全程」一个命令（默认 flow 的 `collect` + 注入模型，一条命令走完采集→建卡→冻结→研究图→委员会并逐节点打印）；`daily`（只补数据、不冻结）与 `decide`（不采集、从卡片池冻结后决策）是给外部定时调度器直接调用同一函数用的，不再各自养一份命令行参数装配。

```bash
python scripts/research_run.py                                    # 全默认：10 只观察池 + data/account.json
python scripts/research_run.py --pool NVDA AAPL                   # 只跑指定标的
python scripts/research_run.py --account data/account.json        # 换账户文件
python scripts/research_run.py --as-of 2026-10-03T18:00:00+00:00  # 历史时点回放（本轮新拉数据被 PIT 拒绝）
```

- **全链需要真实账户。** `data/account.json` 至少含 `cash`/`positions`/`limits`（可带 `regime`、`turnover_cost_estimate`）。缺真实账户时 flow 直接失败而不是退回占位值：Risk Critic 的规定是「缺 limits 或持仓状态不得当作放行」，占位账户只会烧出一轮全 `abstain` 的意图。`data/account*.json` 已进 `.gitignore`，仓库不代存持仓。
- **同时点重跑会被拒。** trace_id 取自 `as_of` 的秒级戳，账本用主键拒绝覆盖历史；调度器应把这条 `ValueError` 当幂等信号（退出码 2），而不是崩溃。
- 实盘模式（不传 `--as-of`）在全部采集完成后才定决策时点，本轮到达的数据本轮即可用；回放模式时点钉死，本轮新拉数据被 PIT 闸门拒绝。
- 产出全部落 `data/research/`：正文分库（news/market/filings.db，内容寻址 append-only）与账本 ledger.db（卡片/快照/trace）；`data/cache/` 下另有可弃的网络备忘层（如 SEC 应答 JSON），不是事实存储。
- 配额与节奏：Alpha Vantage 免费档 25 次/天、段间 15s 节流；coverage 台账记住“问过哪段”不重烧。Windows 下建议直调环境内 `python.exe`（`conda run` 会吞输出）。

## 可视化调试台（`viz/`）

本地单人用的观测页：标准库 HTTP + SSE，只监听 `127.0.0.1`，前端为无构建的原生页面（`viz/server.py` + `viz/readmodel.py` + `viz/static/`）。它**自己不编排**：“跑全程”就是调 `run_research_round`，与命令行、定时调度器同一个函数；只是把模型客户端套一层事件包装，将每个阶段的流转、逐节点的注入输入/工具调用/JSON 输出实时推到前端便于溯源。唯一多出的节奏是“复用已冻结 Snapshot 只重跑研究图/委员会”，用于不重烧采集配额地反复调试。

```bash
python viz/server.py                            # 默认 http://127.0.0.1:8765
python viz/server.py --port 9000                # 换端口
python viz/server.py --runs-dir tmp/viz_runs    # 换观测产物目录
```

页面选中标的池/账户/阶段开关后点运行即可看逐阶段实时流转；历史运行事件流写 `output/viz_runs/<run_id>.jsonl`（Git 忽略）仅供刷新后回看，权威审计仍是账本与 `logs/llm_calls.jsonl`。

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
tests/research/                 阶段 0-3 契约、离线数据、审计与编排入口回归测试
scripts/research_run.py         研究链唯一人类入口（一条命令跑全程；daily/decide 由调度器直接调 flow）
scripts/run_backtest.py         统一回测入口
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
