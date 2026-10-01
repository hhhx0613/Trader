# 提案：面向量化研究与模拟执行的可审计多 Agent 系统

> 状态：讨论稿
> 日期：2026-09-30
> 范围：本提案描述一个毕设方向，不替代 `docs/Plan_v2.md` 的当前权威实现计划。在确认范围与路线后，再将获准的架构变更同步进 `Plan_v2.md`。

---

## 1. 摘要

本项目拟从“以 PPO 收益优化为中心的量化策略”转向一个完整的 **Research-to-Trade Agent** 应用：系统从时点安全（Point-in-Time, PIT）的新闻、财报、行情和账户状态中获取信息，经由职责分明的 LLM Agent 形成、验证和质疑投资论点；再由确定性组合、风控和模拟执行模块把已批准的决策转化为订单；最后以成交、后续收益和复盘结果更新可检索记忆。

本研究不预设 LLM、多 Agent 或 PPO 必然带来收益提升。主要目标是验证：证据追溯、反方审查、分歧驱动的观望/拒绝，以及结果绑定的记忆，能否提高决策过程的可靠性、可解释性和风险调整后的实证表现。

本系统的目标不是复刻某个多 Agent 框架，也不是让多个模型投票预测涨跌；它是一个受限、可审计的 Agent 应用。LLM 只承担研究、验证、质疑和建议，选股规则、仓位、风控、订单与撮合保持由可复现程序控制。

## 2. 背景与问题

现有量化项目的常见两种极端是：

- 只优化预测或强化学习策略的收益，难以说明收益来自真实 alpha、市场 beta、暴露差异还是回测口径；
- 只展示多个 Agent 的讨论过程，缺少可核验的证据、执行约束和统一的样本外评估。

当前项目已具备 PIT 新闻、单 LLM 事件分析、Top-K 选股、公式/ PPO 总暴露、规则风控、T+1 模拟执行和回测基础。但已有实验尚未证明 LLM 新闻选股或 PPO 仓位具有跨窗口的稳定增量。因此，继续将“最大化收益”作为唯一目标会使研究过度依赖不稳定的回测结果。

本提案针对的核心问题是：

> 如何让一个量化交易 Agent 从研究资料走到模拟下单时，能够说明它看到了什么、为何相信、谁提出反对、何时应拒绝交易，以及事后如何被客观验证？

## 3. 目标与非目标

### 3.1 目标

1. 构建从新闻/财报/行情研究、选股、仓位、风控到模拟订单与复盘的完整闭环。
2. 让每一个投资论点绑定原始证据、来源、发布时间和决策时点，保证 PIT 合规与可回放。
3. 通过 Agent 职责分离和反方审查，显式表示证据不足、信息过期和角色分歧。
4. 让系统可输出“买入、持有、卖出、降低暴露、观望、需要人工复核”，而非强制预测或交易。
5. 使用统一股票池、成本、执行时点和滚动样本外评估，检验每个 Agent 机制的边际价值。

### 3.2 非目标

- 不承诺或预设跑赢市场；收益是验证指标，而非唯一成功标准。
- 不在本阶段接入实盘自动下单；执行边界为可审计的模拟盘。
- 不复刻 TradingAgents 式的大规模自由辩论，不以 Agent 数量作为创新点。
- 不让 LLM 自由进行数值计算、确定仓位或绕过程序化风控。
- 不在首版支持多市场、期权、加密货币、跟单社区或复杂知识图谱。
- 不给 Agent 任意文件系统、任意数据库、任意网络、任意代码执行或直接券商下单权限。

## 4. 研究问题

- **RQ1：** 相比单一新闻 Analyst，职责分工与反方审查能否改善事件信号的横截面质量？
- **RQ2：** 证据覆盖率、信息新鲜度和 Agent 分歧度能否识别低质量交易信号？
- **RQ3：** 当系统选择观望/拒绝交易时，能否降低错误交易、换手或尾部风险？
- **RQ4：** 结果绑定的情景记忆能否在不引入未来信息的条件下改善后续研究或校准？
- **RQ5：** 在相同选股、成本和风控下，PPO 总暴露相对波动率目标公式是否有独立的增量价值？

## 5. 系统范围与架构

首版范围限定为：小规模美股候选池、周频调仓、PIT 新闻/财报/行情、T-1 形成信号、T 日开盘模拟执行。

```text
PIT 新闻 / 财报 / 行情 / 账户状态
                │
                ▼
       数据校验与证据索引
                │
      ┌─────────┼─────────┐
      ▼         ▼         ▼
  事件 Agent  基本面 Agent  风险 Critic
      └─────────┼─────────┘
                ▼
      结构化论点与分歧评估
                │
                ▼
       投资委员会 / 裁决器
   （交易、降仓、观望、人工复核）
                │
                ▼
 确定性 Top-K / 权重 / 风控 / 总暴露
                │
                ▼
       订单生成与模拟撮合执行
                │
                ▼
   决策回放、结果归因与情景记忆
```

### 5.1 Agent 职责

| 组件 | 输入 | 输出 | 禁止事项 |
| --- | --- | --- | --- |
| Event Agent | PIT 新闻与公司上下文 | 事件事实、影响方向、证据引用、时效 | 无引用的价格预测 |
| Fundamental Agent | PIT 财报、Event Agent 论点 | 支持/反驳证据、基本面约束 | 代替数值仓位模块 |
| Risk Critic | 论点、账户、风险规则、行情摘要 | 反例、冲突、过期/不可交易标记 | 生成看多论点以凑共识 |
| Committee Agent | 经校验的各 Agent 卡片 | 交易建议、理由、分歧、是否观望 | 绕过证据或风控直接下单 |
| Deterministic Executor | 已批准的结构化决策 | Top-K、权重、订单、模拟成交 | 修改 Agent 结论或跳过风控 |

多 Agent 可以使用同一基础模型，但必须具有不同输入、职责、输出 schema 和权限；“多个 prompt 产生相同分数”不构成有效的协作。

### 5.2 与单 LLM 选股模块的关系

多 Agent 首版的主要业务输出仍是“哪些标的值得进入候选组合，以及是否应观望”，因此它是对当前单 LLM 选股模块的升级，而非取代整个量化引擎。

| 当前单 LLM Analyst | 提案中的多 Agent 研究层 |
| --- | --- |
| 新闻 → 分数/置信度 → Top-K | 多源材料 → 事件论点、基本面验证、风险反证 → 裁决 → Top-K |
| 主要回答看多/看空 | 回答依据、反例、时效、证据是否足够、是否应观望 |
| 输出 `score/confidence` | 输出可引用 ClaimCard、分歧、风险标记和观望原因 |
| 错误主要从最终收益发现 | 可归因到证据、事件理解、风险审查、执行或市场路径 |
| 可直接参与排序 | 可被 Critic 否决，或因证据不足不参与排序 |

因此，当前 Analyst 可自然演进为 Event Agent。多 Agent 扩展的是选股前的研究与审查深度；数值权重、总暴露和订单执行不交给 LLM 自由决定。

### 5.3 结构化信息对象

系统应以可验证的数据对象衔接 Agent 与交易系统，而不是传递自由文本。

```text
EvidenceCard
  source_id, source_type, publisher, published_at, retrieved_at
  symbol(s), original_excerpt, PIT_valid

ClaimCard
  symbol, horizon, direction, strength
  evidence_ids, counter_evidence_ids, freshness, uncertainty

DecisionCard
  rebalance_date, selected_symbols, action
  disagreement, evidence_coverage, abstention_reason, trace_id

OrderCard
  trace_id, symbol, side, target_weight, execution_date
  risk_checks, fill_price, commission, slippage

OutcomeCard
  trace_id, horizon_return, relative_return, drawdown
  review_label, failure_mode, memory_eligible
```

## 6. Agent 应用关键技术

### 6.1 PIT 检索增强与工具调用

Agent 通过受限工具读取数据，而非在 prompt 中一次性塞入全部资料。每次工具调用必须携带决策截止时间，数据层拒绝返回未来资料。检索排序综合相关性、发布时间、来源质量、持仓关联度和事件类型；语义相似度不能替代 PIT 过滤。

#### 最小权限工具集

| 工具 | 功能 | 允许调用者 |
| --- | --- | --- |
| `get_news(symbol, cutoff_time)` | 返回截止时点前的新闻、来源、原文和发布时间 | Event Agent |
| `get_filings(symbol, cutoff_time)` | 返回截止时点前的财报与可引用片段 | Fundamental Agent |
| `get_market_snapshot(pool, as_of)` | 返回程序计算的行情、技术特征和市场状态 | Risk Critic、Committee |
| `get_portfolio_state(as_of)` | 返回现金、持仓、暴露、回撤和近期交易 | Risk Critic、Committee |
| `search_memory(query, as_of)` | 检索时间安全的历史论点—结果案例 | 研究类 Agent、Committee |
| `validate_evidence(ids, cutoff_time)` | 校验证据存在、引用匹配与 PIT 合规 | 程序化裁决前置校验 |
| `risk_check(decision, portfolio)` | 校验集中度、回撤禁买、止损和暴露约束 | 确定性风控模块 |
| `simulate_execution(orders, date)` | 使用统一成本与撮合语义模拟成交 | 确定性执行器 |
| `review_outcome(trace_id, horizon)` | 计算事后收益、相对收益与回撤 | 复盘模块 |
| `write_memory(review)` | 写入已验证的复盘结论 | 复盘模块 |

`rank_candidates`、`generate_orders` 和 PIT 校验属于程序化流程，不应作为 LLM 可随意调用的万能工具。研究 Agent 只能读资料、写论点卡；Risk Critic 只能提出限制或否决；Committee 只能产生 DecisionCard；执行器只消费已批准 DecisionCard。

### 6.2 上下文压缩

压缩采用“原始材料 → 事实卡 → 论点卡 → 调仓简报”四层：

- 原始新闻、财报永久保留，作为审计依据；
- 事实卡只记录可引用事实，不推断投资结论；
- 论点卡绑定方向、期限、支持和反驳证据；
- 调仓简报按 token 预算选取本周最相关的论点与原文片段。

压缩不得丢失主体、时间、数值单位、条件限制和否定信息。

### 6.3 结果绑定的情景记忆

记忆不保存泛化的对话摘要，而是保存“当时论点—引用证据—交易—事后结果—复盘标签”。检索时需同时满足时间安全、标的/行业相关、事件相似和结果可用；无结果或经验证无价值的记忆应降权或过期。

记忆分为：

- 工作记忆：本次调仓材料和账户状态；
- 情景记忆：历史交易及其后续结果；
- 语义记忆：稳定的公司/行业事实和风控规则；
- 程序记忆：如“证据不足必须观望”的固定流程。

### 6.4 分歧、拒答与人工复核

系统不把置信度简单平均。裁决器计算 `evidence_coverage`、`freshness`、`disagreement` 和 `risk_flags`：

- 证据不足、过期或关键事实冲突：观望；
- 风险规则触发：限制加仓或否决；
- 分歧高但证据可补：标记人工复核；
- 证据充分且风险通过：进入确定性选股与仓位流程。

### 6.5 反思与复盘

先由程序计算固定持有期收益、相对候选池收益、成本和回撤，再由复盘 Agent 为论点标注有效性和失败模式。反思只能基于客观结果写入情景记忆，不能以语言自评替代验证。

### 6.6 可靠性与可观测性

- Agent 输出使用 JSON schema/Pydantic 校验；
- 所有论点必须引用有效 EvidenceCard；
- 研究 Agent 没有下单权限，执行器只能消费已批准 DecisionCard；
- 每次运行使用 `trace_id` 串联数据、prompt 版本、模型、Agent 输出、风控和订单；
- 支持在冻结的数据、模型与 prompt 版本下回放决策；
- 记录 token、延迟、缓存命中、失败与成本。

### 6.7 编排框架与技术选型

首版不以 Agent 框架为中心。固定且强约束的工作流应先用原生 Python 函数、明确的状态对象和确定性条件实现，以确保每一步的数据流和权限可被完全理解与测试：

```text
读取 PIT 数据 → Event Agent → Critic → 证据校验/裁决
→ 确定性选股与风控 → 模拟订单 → 复盘
```

推荐技术分层：

| 阶段 | 推荐工具 | 原因 |
| --- | --- | --- |
| P0：单 Agent + Critic | Python、现有 OpenAI-compatible SDK、Pydantic、SQLite | 保持最小复杂度，先固化数据契约、PIT、Trace 和观望机制 |
| P1：条件分支、暂停、恢复、审批 | LangGraph | 仅在确有状态检查点、人工批准、重试和条件路由需求时引入 |
| P2：文本/记忆检索 | SQLite 元数据过滤与 FTS；必要时 Qdrant | 时间、标的和来源过滤优先于语义检索；不为 RAG 提前引入向量库 |
| P3：演示与回放 | FastAPI 或 Streamlit | 最后建设研究报告、订单和 Trace 的展示界面 |

LangGraph 适合后续的“确定性 + Agent”混合、长状态流程，但不应代替领域架构。AutoGen/CrewAI 等对话优先框架适合原型或开放协作；本项目不采用其自由群聊作为核心控制机制，以避免不可预测的对话轮次、成本和状态漂移。所有 Agent 输出应使用 Pydantic/JSON Schema 校验。

## 7. 执行与仓位边界

LLM Agent 负责语义理解、论点形成、证据质疑和裁决建议。数值部分保持可复现：

- Top-K：由结构化 DecisionCard 的合格论点进行确定性排序；
- 个股权重：逆波动率或其他明确公式；
- 总暴露：公式波动率目标为基线，PPO 是可替换的实验模块；
- 风控：仅能缩减或否决，不允许 Agent 凭空提高暴露；
- 执行：T-1 决策、T 日开盘模拟撮合，统一计入滑点、佣金与换手成本。

若 PPO 未通过多种子、滚动样本外与 block bootstrap 验收，应退回公式仓位，而不影响 Agent 应用主线。

## 8. 评测设计

### 8.1 对照与消融

1. VADER、价格动量、随机排序；
2. 当前单 LLM Event Agent；
3. 单 Agent + 确定性风控；
4. 多 Agent，但不使用 Risk Critic；
5. 多 Agent + Critic，但不允许观望；
6. 多 Agent + Critic + 分歧驱动观望；
7. 有/无结果绑定记忆；
8. 相同 Agent 信号下的公式仓位与 PPO 仓位。

所有组使用相同候选池、调仓日、执行时点、权重规则、成本和样本外窗口。

### 8.2 指标

| 层级 | 指标 |
| --- | --- |
| 证据与流程 | PIT 违规数、证据引用有效率、输出 schema 合规率、决策可回放率 |
| 信号质量 | Rank IC、Top-K 相对候选池收益、Top-K 减 Bottom-K、Brier score |
| 拒答机制 | 覆盖率、拒答后被过滤信号的事后表现、观望机会成本 |
| 组合表现 | 年化收益、夏普、最大回撤、平均暴露、换手、交易成本 |
| 工程代价 | token 成本、延迟、缓存命中率、工具调用失败率 |

结论以滚动样本外窗口、多种子（适用于 PPO）和按周 block bootstrap 的区间为准；单一回测路径只可标记为 provisional。

## 9. 与相关工作的差异

| 工作 | 主要重点 | 本项目的差异 |
| --- | --- | --- |
| TradingAgents | 以交易公司角色模拟、研究员辩论和最终交易为中心 | 聚焦 PIT 证据链、分歧驱动观望、确定性执行边界和决策回放，而不是增加角色数量 |
| AI-Trader（HKUDS） | Agent-native 信号平台、实时参与、跟单与社区 | 聚焦单个研究型 Agent 的离线可复现、模拟执行和系统级可靠性评测，不建设信号市场 |
| FinRL | DRL 交易环境、训练、回测与执行框架 | 将 RL 降为可替换的总暴露模块，重点研究 Agent 研究质量与安全决策 |
| FinMem | 分层记忆、角色设定和反思 | 记忆强制绑定论点、原始证据、交易和客观结果，并单独做增量消融 |
| FinAgent | 工具增强的多模态金融 Agent | 不让 LLM 自由承担数值计算；强调结构化证据、权限边界与可回放订单 |

## 10. 预期贡献

1. 一个从 PIT 研究材料到模拟订单、复盘与记忆的端到端量化 Agent 应用；
2. 一套 EvidenceCard/ClaimCard/DecisionCard/OrderCard/OutcomeCard 的可审计决策数据模型；
3. 一个基于证据覆盖率、信息时效与角色分歧的观望/拒答机制；
4. 一套面向交易 Agent 的统一评测协议，覆盖信号、组合、过程可靠性和工程成本；
5. 对多 Agent、记忆、拒答和 PPO 模块的独立消融结论，包括负结果。

## 11. 实施优先级

### P0：闭环与审计基础

1. 定义五类结构化卡片与 `trace_id`；
2. 将当前 LLM Analyst 包装为 Event Agent；
3. 记录原始证据、发布时间、prompt/模型版本与决策回放信息；
4. 将既有 Top-K、风控与回测接入 DecisionCard；
5. 明确模拟订单生命周期与成交日志。

### P1：研究可靠性

1. 新增 Risk Critic；
2. 实现证据覆盖率、时效和分歧指标；
3. 实现观望/人工复核状态；
4. 先完成单 Agent vs Critic vs 拒答的消融。

### P2：学习与扩展

1. 新增 Fundamental Agent；
2. 实现结果绑定情景记忆与复盘；
3. 评估记忆增益；
4. 仅在需要时引入向量检索、知识图谱或 PPO 旁路。

## 12. 风险与控制

| 风险 | 控制措施 |
| --- | --- |
| 为了“多 Agent”重复推理、增加成本 | 每个 Agent 具有不同职责、输入和否决权；以消融证明保留价值 |
| LLM 幻觉或伪造引用 | 引用必须对应本地 EvidenceCard；程序校验时间和来源 |
| 未来函数/回测泄漏 | PIT 数据截断、T-1/T 日执行、冻结数据版本与回放 |
| 收益不稳定 | 不把收益作为唯一成功条件；报告信号质量、拒答与风险指标 |
| 范围失控 | 首版只做小股票池、周频、模拟执行与 3–4 个 Agent |
| PPO 样本不足 | PPO 仅作为独立仓位消融；未通过验收即退回公式基线 |
| 框架或 Agent 对话制造额外复杂度 | 先用原生 Python 状态机；仅在出现真实的检查点、条件路由和恢复需求后引入 LangGraph |
| AI 编码掩盖架构退化 | 由人工维护架构地图、不变量和最小设计；用 schema、测试和 diff 审查守住边界 |

## 13. 架构治理与 AI 编码原则

AI 可以协助实现局部模块、补齐测试、生成重复性转换代码和审查 diff；但架构边界、数据契约、实验验收标准和 Go/No-Go 决定必须由研究者掌握并批准。

### 13.1 人工维护的架构地图

研究者应能脱离代码说明并绘制以下主链路：

```text
PIT 数据 → Evidence/Claim → Agent 裁决 → Top-K/仓位/风控
→ 订单 → 撮合 → Outcome → 复盘记忆
```

且明确以下不可破坏的不变量：

- Agent 不得读取决策时点之后的数据；
- 每项 Claim 必须能回到有效 Evidence；
- Agent 无权绕过风控或直接修改账户；
- 风控只能限制或否决，不得凭空增加风险；
- 每个订单必须能回放至 DecisionCard、输入数据、模型/prompt 版本和成本；
- 每一项实验性能力都必须保有可替换的简单基线。

### 13.2 最小设计—实现—验证流程

每次有影响的改动遵循以下顺序：

1. 先描述目标、涉及模块、输入输出、不变量、失败模式与验收指标；
2. 在获得确认后，把架构变更更新至权威 `Plan_v2.md`；
3. 再由 AI 或人工实施局部变更；
4. 审查 diff，解释每处改动为何存在、接口如何变化；
5. 增补边界测试、PIT 测试和回归测试；
6. 用结果决定保留、修正或删除该能力，而不是叠加兼容层。

### 13.3 架构体检

每完成一个模块，至少回答：

1. 它唯一负责什么？
2. 它是否依赖了不该依赖的层？
3. 它是否绕过了统一 PIT、风控、订单或回测入口？
4. 它是否创建了第二套缓存、评分、订单或记忆概念？
5. 删除它时，系统失去的明确能力是什么？

若这些问题无法清楚回答，应停止继续添加功能，先收敛或删除复杂度。

## 14. 成功标准

项目完成不要求“盈利最大化”，而要求：

1. 每笔模拟订单可回放至当时的 PIT 原始证据、Agent 结论、风控判断和执行成本；
2. 系统能够在证据不足、过期、冲突或风险超限时稳定输出观望/否决；
3. 完成预定义的对照、消融、样本外和成本评估；
4. 对多 Agent、记忆、拒答和 PPO 是否有增量给出有证据支撑的结论；
5. 系统能演示“输入当期信息 → 研究 → 交易/观望 → 模拟执行 → 复盘”的完整闭环。

## 15. 参考方向

- Xiao et al. *TradingAgents: Multi-Agents LLM Financial Trading Framework*, arXiv:2412.20138. https://arxiv.org/abs/2412.20138
- Fan et al. *AI-Trader: Benchmarking Autonomous Agents in Real-Time Financial Markets*, arXiv:2512.10971. https://arxiv.org/abs/2512.10971
- Liu et al. *FinRL: Deep Reinforcement Learning Framework to Automate Trading in Quantitative Finance*, ICAIF 2021. https://openfin.engineering.columbia.edu/sites/default/files/content/publications/3490354.3494366.pdf
- Yu et al. *FinMem: A Performance-Enhanced LLM Trading Agent with Layered Memory and Character Design*, arXiv:2311.13743. https://arxiv.org/abs/2311.13743
- Zhang et al. *A Multimodal Foundation Agent for Financial Trading*, KDD 2024. https://arxiv.org/abs/2402.18485
- Yao et al. *ReAct: Synergizing Reasoning and Acting in Language Models*, ICLR 2023. https://arxiv.org/abs/2210.03629
- Shinn et al. *Reflexion: Language Agents with Verbal Reinforcement Learning*, NeurIPS 2023. https://arxiv.org/abs/2303.11366
- LangGraph Documentation. *LangGraph: long-running, stateful agent orchestration*. https://langchain-ai.github.io/langgraph/reference/
- Pydantic Documentation. *JSON Schema and validation*. https://pydantic.dev/docs/validation/2.3/usage/json_schema/
