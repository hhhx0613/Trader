# 开发实施计划

## 1. 用途与使用方式

本文件用于后续 Agent 追踪任务、依赖、验收和阶段交接。Plan.md 是唯一架构权威；本文件不重新定义架构，发生冲突时以 Plan.md 为准。

每次开始任务时，执行 Agent 必须：

1. 将对应任务标记为进行中，并记录 trace_id、分支或提交。
2. 仅实施当前阶段的已批准事项；需要新增架构选择时，先更新 Plan.md。
3. 完成后记录验证命令、结果和限制，并标记完成或阻塞。

当前状态：新主线所有阶段均未开始。现有单 LLM、Top-K、逆波动率、PPO 和模拟撮合代码是可复用基础或对照，不代表新主线完成。

## 2. 固定技术选型

### 2.1 运行时、框架与边界

- Python 3.10+；新模块保持 Python 3.10 兼容。
- LangGraph：编排 DecisionGraph 与 OutcomeGraph，处理条件分支、失败恢复和人工复核中断。
- Pydantic v2：定义所有跨节点对象；每个对象包含 schema_version、trace_id、created_at。
- numpy/pandas：市场特征、波动率与协方差计算。
- cvxpy：风险预算参考分配的约束投影。
- pytest：Provider、Gateway、Graph、PortfolioPolicy 的离线 fixture 测试。

LangGraph 只保存单次运行的临时图状态，不能作为事实、审计记录或跨进程恢复的唯一来源。跨日恢复必须从审计账本与冻结 Snapshot 重建。Agent 不持有聊天记忆，也不直接访问 HTTP、文件或数据库。

### 2.2 存储与数据库

采用 SQLite，不引入服务端数据库。数据库使用 WAL、外键约束、显式事务和版本化迁移。

| 位置 | 职责 | 边界 |
| --- | --- | --- |
| data/cache/llm/llm_cache.db | 已有 LLM 调用缓存 | 只用于复用，不能作为审计或长期记忆。 |
| data/research/research_ledger.db | 新增追加式审计账本 | 保存对象元数据、关联 ID、版本、状态和原因码；禁止覆盖已完成 trace。 |
| data/research/raw/<sha256>/ | 原始新闻、披露、行情响应 | 账本保存路径、哈希、来源和可得时间；大文本不写 SQLite BLOB。 |
| output/backtest_<run_id>/ | 单次回测产物 | 保持不可覆盖；manifest 关联 trace、配置和策略版本。 |

审计账本至少保存：traces、snapshots、evidence_cards、claim_cards、research_packets、portfolio_intents、intent_constraints、target_portfolios、risk_projected_portfolios、order_plans、deferred_trades、fills、outcome_cards、review_labels。

### 2.3 目录归属

| 目录 | 内容 |
| --- | --- |
| core/research/ | contracts、store、snapshot、DataGateway、审计回放 |
| core/data/ | News、Filing、Market Provider 与数据规范化 |
| agents/research/ | Event、Fundamental、Market、Risk Critic、Committee |
| graphs/ | LangGraph 的 DecisionGraph 与 OutcomeGraph |
| core/portfolio/ | IntentConstraintBuilder、RiskBudgetAllocator、VolatilityTarget |
| core/risk/ | RiskProjection、CostGate、OrderPlan |
| tests/research/ | 数据、账本、Gateway、Graph 测试 |
| tests/portfolio/ | 权重约束、风险、成本、订单测试 |

旧路径留在原位置作为对照。新主线不得通过兼容层调用旧 Top-K 或 PPO 决定订单。

### 2.4 现有代码迁移落点

| 现有实现 | 新主线用途 | 阶段 |
| --- | --- | --- |
| core/data/news_data.py | 新闻拉取、缓存与 PIT 访问；扩展为 EvidenceCard、去重和转载过滤 | 1 |
| core/data/market_data.py | OHLCV 与技术特征；补充版本和市场状态 Snapshot | 1 |
| agents/llm_analyst.py | 模型调用、提示词和缓存；封装为带引用的 ClaimCard 输出 | 1–2 |
| agents/stock_selector.py | 单资产调度；演进为 ResearchPacket 构建 | 2 |
| agents/decision_func.py | 逆波动率与公式总暴露；保留为组合层对照 | 3 |
| core/multi_stock_engine.py | T-1/T 日撮合和账户状态；接收 OrderPlan 与 trace | 3 |
| core/risk_manager.py | 风控和成本检查；演进为投影、原因码与延后订单 | 3 |
| core/data/llm_cache_db.py | LLM 缓存；与审计账本和案例检索分离 | 1、4 |
| core/ppo/ | PPO 对照策略 | 3、5 |

## 3. 分阶段任务

本文件是唯一的分阶段交付清单；阶段 4 与阶段 5 分别验收“结果复盘与回放”和“实验矩阵与论文交付”。

### 阶段 1：可重放的数据与审计地基

状态：完成

- 开始日期：2026-10-03
- trace_id：stage1-20261003-bootstrap
- 分支或提交：当前工作树（提交前待补充）
- 完成日期：2026-10-03
- 实现范围：`core/research/` 的 Pydantic 数据契约、内容寻址原始载荷、SQLite WAL 追加式账本、PIT 规范化、Snapshot Builder 与只读审计 DataGateway；不包含研究 Agent、交易意图或订单。
- 验证：`python -m pytest tests\\research\\test_stage1_snapshot.py -q --basetemp D:\\Codes\\Trader\\.stage1_pytest_verified`（6 passed）；`python -m pytest tests\\test_risk_manager.py -q --basetemp D:\\Codes\\Trader\\.risk_pytest_verified`（2 passed）；`python -m compileall -q core\\research` 通过。
- 审计样本：离线 fixture 的 `trace-1`；测试为隔离临时数据库，不写入项目研究账本。
- 已知限制：新闻、披露和行情的线上拉取仍由既有 Provider 提供；阶段一只接收其已获取记录并做规范化、PIT 校验和冻结，尚未接入任何 Agent 或线上调度。
- 下一阶段前置条件：在冻结 Snapshot 上只经 DataGateway 读取事实；不得绕过账本或让 Agent 直接访问 Provider。

目标：给定股票池和 as_of，离线构建并重放 PIT 合格的 ResearchSnapshot。

- [x] 锁定 LangGraph、Pydantic v2、cvxpy 依赖与版本；可选依赖不得在普通模块导入时强制加载。
- [x] 建立 contracts、追加式 store、SQLite 迁移和 research_ledger.db。
- [x] 建立 raw payload 存储、内容哈希、引用关系与保留规则。
- [x] 规范化 News、SEC Filing、Market Provider；实现 available_at PIT 校验、去重、转载过滤和离线 fixture。
- [x] 实现 Snapshot Builder，冻结股票池、来源版本、新闻、披露、行情与账户状态。
- [x] 实现只读 DataGateway；每次查询校验 symbol、snapshot_id、as_of 并记入审计账本。

验收：断网条件下可重建相同 Snapshot；未来数据、无可得时间数据和重复转载被拒绝并有原因码；EvidenceCard 可定位原始文件与哈希。

不做：不调用研究 Agent，不生成交易意图，不接入真实下单。

### 阶段 2：无状态多 Agent 研究与 Committee

状态：未开始

目标：从冻结 Snapshot 形成可引用、可反驳、可观望的 PortfolioIntent。

- [ ] 实现 Event Agent 的每日 triage 与周度 ClaimCard；每项事实绑定 EvidenceCard ID。
- [ ] 实现 Fundamental Agent 的披露/XBRL 核验，Market Agent 的程序化特征解释。
- [ ] 实现 Risk Critic 的交叉质疑、证据缺口与软否决。
- [ ] 实现 Committee：输出 long、hold、reduce、exit、abstain、动作强度、优先级、持有期、Card ID 与不交易理由。
- [ ] 用 LangGraph 实现 DecisionGraph：Snapshot、PerAssetResearch、ThesisBook、Committee；支持数据补查、观望、人工复核和失败恢复。
- [ ] 保存每个节点的输入对象 ID、提示词、模型、token、延迟、schema 结果和失败原因。

验收：同一冻结 Snapshot 可重放；每个非 abstain 动作有支持证据和已检查反证；删除关键 EvidenceCard 后下游对象失效或重算。

不做：Agent 不访问裸 API，不输出权重、收益预测或订单，不把聊天记录当研究记忆。

### 阶段 3：确定性组合政策与模拟执行

状态：未开始

目标：将五个动作转为完整、可执行、可审计的股票加现金组合。

- [ ] 实现 IntentConstraintBuilder：动作、动作强度、优先级和旧仓位转为候选准入、权重边界和交易许可。
- [ ] 实现 RiskBudgetAllocator：逆波动率参考、协方差、行业/单股、流动性与换手约束；不包含收益预测项。
- [ ] 实现 VolatilityTarget：用预估组合波动率计算总股票暴露；现金为剩余权重；增加总暴露限速，降风险即时执行。
- [ ] 实现 RiskProjection：数据有效性、强制退出、集中度/流动性上限、组合风险/回撤限制；输出只能减风险的 RiskProjectedPortfolio。
- [ ] 实现 CostGate：强制交易、no-trade band、最小订单、参与率、现金、换手和订单层成本复核；输出 OrderPlan 与 DeferredTrade。
- [ ] 接入现有 T-1 决策、T 日开盘模拟撮合，记录成交、未成交和成本。

验收：相同 Intent、行情、配置必定得到相同 TargetPortfolio；RiskProjection 与 CostGate 不新增风险；成交或延后交易可回溯到 Intent、边界、目标、投影和原因码。

不做：PPO 不进入主线；等权、纯逆波动率、PPO 只作为同接口对照策略。

### 阶段 4：结果复盘、受控案例与完整回放

状态：未开始

目标：建立决策到客观结果的闭环，不让历史案例变成隐性的仓位预测器。

- [ ] 实现 OutcomeGraph：持有期到期、退出或 thesis 失效时写入不可修改 OutcomeCard；NoTrade 同样产生反事实结果。
- [ ] 实现 Review Agent：只读原始证据、Intent、成交与 Outcome，标记有效性或失败模式。
- [ ] 实现受控案例检索：只返回证据完整、已到期、复盘完成并带 trace 的少量案例，供 Committee 识别失败模式。
- [ ] 实现离线 replay：任意 trace 在不访问实时数据时重建图输入、节点输出、目标组合、订单和 Outcome。
- [ ] 完成存储恢复、迁移、损坏数据与跨日运行测试。

验收：未到期记录不能被检索；完整 trace 可离线回放；案例检索不改变 PortfolioPolicy 的确定性输入输出。

### 阶段 5：实验矩阵与论文交付

状态：未开始

目标：分别验证 Agent 研究层与 Portfolio 层的机制价值。

- [ ] 固定 PIT、股票池、调仓日历、成本、风险限额、执行时序和实验 manifest。
- [ ] 研究层消融：无 LLM、单 LLM、无 Critic、多 Agent 无观望、完整多 Agent。
- [ ] 组合层对照：等权、纯逆波动率、动作约束下的成本感知风险预算、PPO。
- [ ] 同时报审计指标：引用有效率、冲突率、观望率、回放完整率、延迟和调用成本。
- [ ] 同时报经济指标：收益、Sharpe、回撤、暴露、换手、成本、Rank IC。
- [ ] PPO 必须通过多随机种子、滚动样本外窗口和 block bootstrap；否则只标为实验性对照。
- [ ] 交付架构图、数据契约、消融表、失败案例和可复现运行说明。

验收：不以单一短窗口收益主张系统或选股有效；每个结论可定位 manifest、配置、数据截止时间和代码版本。

## 4. 阶段交接记录

每完成一个阶段，在该阶段标题下追加：

- 状态：未开始 / 进行中 / 完成 / 阻塞
- 完成日期：
- 实现范围：
- 验证：命令与结果
- 审计样本：trace_id 或 run_id
- 已知限制：
- 下一阶段前置条件：

## 5. 变更规则

- 数据契约、数据库表、图节点、PortfolioPolicy 接口变更属于架构变更，先更新 Plan.md。
- 仅增加已批准阶段内的任务、测试或进度记录时，更新本文件即可。
- 新旧路径不得共同决定同一笔订单。
- 未通过阶段验收，不开始下一阶段的训练或回测。
