# 可审计量化研究与模拟执行系统

当前仓库保留 LLM、VADER 和 PPO 的统一回测基线；项目将逐步演进为从 PIT 数据研究到模拟执行的可审计多 Agent 应用。目标不是直接追求收益最大化，而是让每一项决策都能追溯其可得证据、研究论点、风险审查、成本约束和最终结果。

权威开发计划见 [docs/Plan.md](docs/Plan.md)；历史实验以 [docs/experiment_index.csv](docs/experiment_index.csv) 为唯一结构化登记册。

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

### 阶段 4 组合政策（离线）

`core/decision/` 已提供确定性的 `PortfolioPolicy`：`PortfolioIntent -> IntentConstraints -> TargetPortfolio -> RiskProjectedPortfolio -> OrderPlan`。它使用动作边界、逆波动率参考分配、协方差波动率目标、只减风险的硬约束投影和成本门控；所有中间对象均可写入 `ResearchLedger`。**组合政策参数（单仓上限、分配/波动率/风险/成本阈值与模型、`config_version`）集中管理在根 `config.py`**（无 `PORTFOLIO_` 前缀的模块级常量），`data/account*.json` 只承载账户状态；`risk_state`（峰值/回撤/止损/锁仓）由 daily 节奏从 PIT 日线重算并回写账户。研究链入口（flow）在 Committee 后自动调用组合政策，旧回测引擎不调用它。

### 阶段 5 模拟执行（离线）

`core/decision/` 的 `SimulatedExecution`（与组合政策同一包，旧 `core/execution/` 已合并到 `core/decision/`）只接收冻结的 `OrderPlan`：T-1 形成订单，T 日按开盘价、成交量参与率、滑点、佣金、现金与持仓撮合，并输出显式 `Fill`（完整、部分或延后）。它不重选标的、不重算权重，旧 Top-K/PPO 回测路径也不调用它。人工调用 `execute` 模式才会在下一可交易日写入 Fill，绝不在 `decide` 当天伪造成交。

### 阶段 6 OutcomeReviewFlow（人工复盘）

`review` 模式只读取已冻结的 `OrderPlan`、实际 `Fill` 和 PIT 行情，在 episode 到期、完全退出或 thesis 显式失效时结算唯一一张 `OutcomeCard`；没有成交时明确记录为反事实，不能当作真实收益。可选 Review Agent 只能解释完整客观结果；只有非 `abstain` 的复盘标签才可作为受控案例检索。项目不安装周频任务，人工按需要运行：

```powershell
python scripts/backtest.py --phase review --order-plan-id op_xxx --as-of 2026-10-06T20:00:00+00:00
python scripts/backtest.py --phase review --order-plan-id op_xxx --as-of 2026-11-01T20:00:00+00:00
python scripts/backtest.py --phase review --trigger thesis_invalidated --order-plan-id op_xxx --as-of 2026-11-01T20:00:00+00:00
```

`--phase review` 会在给出 `--order-plan-id` 时先做 T+1 `execute` 成交、再扫描到期 Episode 结算；只给 `--as-of` 不给订单则只扫描已挂起到期的 Episode。

若要从本地历史 news/market CSV 完整回放一遍研究→执行→复盘链路（诊断链路接线与回归，非实时可得性或 alpha 证明），运行 `backtest.py` 的默认 `full` 环节：

```powershell
python scripts/backtest.py --pool AAPL --start 2026-05-01   # 从缓存 CSV 逐交易日闭环，force 默认开
```

`full` 逐交易日导入新闻 EvidenceCard、逐周补一张程序生成的 Market EvidenceCard，驱动 `run_research_round -> T+1 SimulatedExecution -> OutcomeReviewFlow`，研究节点与 Risk Critic 使用项目配置的真实 `LLMClient`；缺缓存标的先跑 `python scripts/backtest.py --phase collect --pool <SYM>` 补数据。历史新闻不带当时接收时间，导入时按 `available_at = published_at` 的明示回放近似处理；为稳定验收 Outcome 记忆链，`force`（默认开，`--no-force` 关）会让 Acceptance Harness 在 Critic 未否决时强制首轮产生一笔有本轮 ClaimCard 支撑的 `long/start`，并替代 Review 的 `memory_eligibility` 与后续 Committee 的 `calibration_episode_ids`，终端与 rationale 均带 `FORCED_ACCEPTANCE` 标记——这些不是 LLM 判断，其余节点不得伪造响应。`full` 与其余环节都统一写生产账本 `data/research/`，用 `python viz/server.py` 直接只读回放，或在 viz 前端选 `full` 节奏在线重放并逐交易日 SSE 推送。

`--trigger exit|thesis_invalidated` 必须由人工明确给出；系统不会让模型自行宣布 thesis 失效。Committee 只收到同标的、同冻结 regime 的已验证 Outcome 摘要，它们不能写入本轮事实引用字段或覆盖 Risk Critic。

可注入模型的 Review Agent 不计算收益，也没有调参/下单权限；它只为单个已终结的持仓 episode 形成带冻结证据引用的历史反思摘要，决策、执行与证据材料在入 prompt 前沿账本物化。摘要随已验证案例注入未来同标的、同 regime 的 Committee prompt，经验影响只能经 `calibration_episode_ids` 显式引用，不得写入当前事实引用字段；越界引用、模型失败或证据不足都会使该案例降级为 `abstain`，不得注入。

研究全流程在 Committee 后会继续运行组合链：`agents/flow.py` 从 `config.py` 政策参数与 PIT 行情、账户状态构造输入，产出 `IntentConstraints`/`TargetPortfolio`/`RiskProjectedPortfolio`/`OrderPlan` 四个对象并写入账本；viz 调试台的「组合政策」阶段与账本回放可直接展示。任何一个行情、账户或限额输入缺失均会拒绝本轮组合决策，绝不填充默认值。

### 阶段 2 研究图谱

`agents/research/` 提供按标的隔离的 LangGraph，拓扑为 `(Event -> Fundamental) || Market -> Risk Critic -> ResearchPacket`：Event 先确立或弃权论点，再分叉两路——Fundamental 用独立财报证据验证 Event 已确立的论点；Market 是独立分支，Event 弃权时不会被硬风控闸掉（例如硬风险或纯价格触发场景）。`event_agent.py`、`fundamental_agent.py`、`market_agent.py`、`risk_critic.py` 各自承担对应研究职责；`claim_agent.py` 只是它们共用的带引用 ClaimCard 校验边界。

研究节点采用「注入 hook + 单工具补查」：每个 Agent 在调用模型前先执行注入 hook，把自己那一份冻结批次（Event 读 news、Fundamental 读 filing、Market 读 market/peer/regime，并把 Event 论点作只读语境注入以供判断价量确认/背离）无条件拼进初始 prompt，因此模型默认单轮即可产出 ClaimCard，不再花一轮工具去「要」本就该看到的数据；模型唯一可自主发起的只读工具是 `search_evidence`（同维度定向补查：`ResearchTools` 按节点锁定证据类别，Event 只能搜 news、Fundamental 只能搜 filing、Market 只能搜 market，维度分工不给补查开后门），`tools.py`（`ResearchTools`）一遍实现其预算/校验/截断/trace 记账，Agent 文件只声明挂载哪些工具与本节点可见的证据类别。Risk Critic 是例外：仓位/换手成本/regime/peer 四项固定上下文全部注入、不挂载任何工具，以单轮 `chat_json` 输出 verdict。证据仍不足时模型走一次性 `data_request` 弃权而非无限补查。

图谱只通过 `core/database/` 的 `DataGateway` 读取 Snapshot 绑定数据，不生成权重、订单或回测，也绝不触网：`DataGateway` 只读账本与原始载荷库。采集与冻结发生在 Snapshot 之前，统一由 `agents/flow.py` 编排（见「运行入口」）。离线验收套件：`python -m pytest tests/research -q`。

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

编排本体在 `agents/flow.py` 的 `run_research_round(FlowOptions, model=...)`：阶段门控与 trace_id 生成只在这一处。`scripts/backtest.py` 是主线唯一的回测/研究人类入口（合并原 `research_run.py` 与 `full_flow_backtest.py`），用 `--phase` 选环节、默认 `full`：`collect` 只联网补数据入正文库、`evidence` 日频建卡入池（= flow daily）、`decision` 冻结→研究图→委员会→OrderPlan（= flow decide + 注入模型）、`review` 成交+到期结算复盘、`full` 多周闭环回放。`daily`/`decide`/`execute` 等单节奏仍由外部定时调度器直接调用 `run_research_round` 同一函数，脚本不各养一份命令行装配。

```bash
python scripts/backtest.py --phase decision --pool NVDA AAPL      # 冻结→研究图→委员会→OrderPlan（需 data/account.json）
python scripts/backtest.py --phase decision --as-of 2026-10-03T18:00:00+00:00  # 历史时点回放（本轮新拉数据被 PIT 拒绝）
python scripts/backtest.py --phase evidence --as-of 2026-10-09T20:00:00+00:00  # 日频建卡入池，不冻结、不跑模型
python scripts/backtest.py                                        # 默认 full：从缓存 CSV 的多周闭环回放
```

- **全链需要真实账户。** `data/account.json` 至少含 `cash`/`positions`/`limits`（可带 `regime`、`turnover_cost_estimate`；`risk_state` 由 daily 从 PIT 日线重算并回写，不要求手填）；组合政策参数不在账户文件里，统一读自 `config.py`。缺真实账户时 flow 直接失败而不是退回占位值：Risk Critic 的规定是「缺 limits 或持仓状态不得当作放行」，占位账户只会烧出一轮全 `abstain` 的意图。`data/account*.json` 已进 `.gitignore`，仓库不代存持仓。
- **同时点重跑会被拒。** trace_id 取自 `as_of` 的秒级戳，账本用主键拒绝覆盖历史；调度器应把这条 `ValueError` 当幂等信号（退出码 2），而不是崩溃。
- 实盘模式（不传 `--as-of`）在全部采集完成后才定决策时点，本轮到达的数据本轮即可用；回放模式时点钉死，本轮新拉数据被 PIT 闸门拒绝。
- 产出全部落 `data/research/`：正文分库（news/market/filings.db，内容寻址 append-only）与账本 ledger.db（卡片/快照/trace）；`data/cache/` 下另有可弃的网络备忘层（如 SEC 应答 JSON），不是事实存储。
- 配额与节奏：Alpha Vantage 免费档 25 次/天、段间 15s 节流；coverage 台账记住“问过哪段”不重烧。Windows 下建议直调环境内 `python.exe`（`conda run` 会吞输出）。

## 可视化调试台（`viz/`）

本地单人用的观测页：后端 FastAPI + SSE（`uvicorn` 启动），只监听 `127.0.0.1`；前端在 `viz/web/`（Vite + React + TypeScript），构建产物 `viz/web/dist/` 由 `server.py` 同源托管，Node 只在构建期使用（Python 侧为此只多 `fastapi`/`uvicorn` 两个包）。它**自己不编排、不复制阶段门控**：`collect`/`decide` 调 `run_research_round`，`evidence` 调 flow `daily`，`review` 调 flow `execute` + `OutcomeScheduler.scan`，`full` 复用 `scripts/backtest.py` 抽出的 `run_replay(...)`——终端 print 与这里的 SSE 是同一事件流的两个 sink，与命令行、定时调度器全用同一批函数；只是把模型客户端套一层事件包装，将每阶段流转、逐节点注入输入/工具调用/JSON 输出、后半程 `Fill`/`Episode`/`OutcomeCard`/`ReviewLabel` 实时推到前端。唯一 flow 没有的直接调用是「复用已冻结 Snapshot 只重跑研究图/委员会」，用于不重烧采集配额地反复调试。

两种起法，选一种。

**开发态（推荐，只有一个访问口）**——浏览器只开 http://127.0.0.1:5173，Vite 把 `/api`（含 SSE）代理到 8765；改前端保存即刷新：

```powershell
# 终端 1：后端 8765 —— /api + SSE + 真正执行 flow，必须常驻
cd d:\Codes\Trader
& D:\Miniconda3\envs\trader\python.exe -u viz\server.py

# 终端 2：前端 5173 —— 编译 TSX + 热更新 + 把 /api 代理给 8765
cd d:\Codes\Trader\viz\web
npm run dev
```

首次要先装一次依赖：`cd viz/web && npm install`（依赖有变时再跑）。

**单进程形态**——不热更新，只留 8765，页面由 `server.py` 直接托管 `dist/`：

```bash
cd viz/web && npm run build                     # 改过前端才需要：产出 dist/
python viz/server.py                            # 打开 http://127.0.0.1:8765
python viz/server.py --port 9000                # 换端口（需同步改 viz/web/vite.config.ts 的 proxy target）
python viz/server.py --runs-dir tmp/viz_runs    # 换观测产物目录
python viz/server.py --ledger <某份账本副本>.db --raw <对应正文库目录>  # 只读回放指定账本（缺省指向 config 的研究账本）
```

端口分工：**8765 是后端**（FastAPI：账本查询、SSE 事件流、执行 flow、托管 `dist/`），**5173 是前端**（Vite dev server，只负责编译、热更新与转发 `/api`）。页面与接口同源，因此两侧都不需要 CORS；未构建 `dist/` 时直接访问 8765 会返回 503 并提示先构建。

页面选中标的池/账户/阶段开关后点运行即可看逐阶段实时流转（`full`/`review` 会展开回放面板填 start/end/force 与复盘 OrderPlan）；裸 ID 的 chip 会顺 `/api/digest` 批量解析成一行摘要（不再只显短 ID，点开抽屉仍看全文）。事件流按「决策日分隔条 → 阶段收口 → 模型节点 → 产出对象」组织，只读渲染层会折叠同一事实的重复表达（带 `stage` 的回放日志行、已收口的「阶段开始」、同阶段按日重发的「阶段完成」（折进首次行的「历次收口」）、已有模型节点的阶段（研究图/委员会）的阶段行）；事件本身仍完整进 SSE 与 JSONL，顶部阶段卡的状态与耗时也不受影响（一次 54 事件的两周回放只画 23 行）。历史运行事件流写 `output/viz_runs/<run_id>.jsonl`（Git 忽略）仅供刷新后回看，权威审计仍是账本与 `logs/llm_calls.jsonl`（每次模型调用带 `trace_id/node/symbol/call_id` 关联键，与事件流的 `node_input/node_output` 同一 `call_id` 对齐，供审计 agent 回溯每个节点当时看到的证据）。

「账本回放」Tab 直接读账本，覆盖阶段 1-6 全部对象：除前半程（Snapshot→证据/论断/研究包→意图→组合政策→OrderPlan）外，还展示后半程 `Fill`/`PositionEpisode`/`OutcomeCard`/`ReviewLabel`——点开 Episode 详情抽屉可顺着「结果卡→复盘标签→成交→引用该案例的意图」逐跳溯源，PortfolioIntent 抽屉则反查它经 `calibration_episode_ids` 引用的已验证案例，即「outcome→review→记忆注入→被 Committee 引用」这条经验闭环在前端可视化。`--ledger/--raw` 为只读覆盖，缺省仍指向 `config` 的研究账本；回测产物账本用该参数即可直接观测，不改默认路径。

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
agents/flow.py                  全流程编排入口（collect→research→committee→decision→fill→outcome）
agents/research/                per-asset LangGraph 与 Committee（事件/基本面/行情/风险/claim/tools/prompting）
agents/outcome/                 阶段 6 Review Agent
core/                           数据、指标、回测、风控与 PPO
core/database/                  跨阶段事实层：契约、Snapshot、原始载荷、审计账本与只读 DataGateway
core/collect/                   阶段 1 三采集器（market/news/filings）
core/decision/                  阶段 4-5 组合政策与模拟执行（PortfolioPolicy+SimulatedExecution）
core/outcome/                   阶段 6 Outcome 计算与调度（OutcomeCalculator+OutcomeScheduler）
core/ppo/                       PPO 训练与预测（decision 的择时输入）
utils/                          LLM 客户端、日志与 LLM 缓存 SQLite
tests/research/                 阶段 0-6 契约、离线数据、审计、组合、执行与结果回归测试
scripts/backtest.py             主线统一回测/研究入口（--phase full/collect/evidence/decision/review，默认 full）
scripts/run_backtest.py         统一回测入口
scripts/train_ppo.py            PPO 训练入口
models/                         PPO 模型归档
docs/Plan.md                    唯一权威开发计划
docs/experiment_index.csv       实验结构化登记册
output/                         单次回测原始证据
data/cache/                     本地缓存（不提交 Git）
data/research/                  三类研究数据的原始载荷和审计账本（不提交 Git）
```

后续工作顺序以 [docs/Plan.md](docs/Plan.md) 为准。
