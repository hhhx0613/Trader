# 相关工作与竞品对比：七位开源邻居的定位、差异与可借鉴之处

> 用途：论文/毕设提案 §6"与相关工作的区别"的详细底稿，兼作工程选型备忘。
> 更新：2026-10-09
> 权威架构仍以 [Plan.md](Plan.md) 为准；本文件不引入新的架构决策，只描述本项目与外部项目的**定位差异**。

---

## 0. 摘要

七个项目按研究范式可分为三层：

- **数据基础设施层**：OpenBB —— 统一多源金融数据的适配器，不做决策。
- **多 Agent 决策层**：TradingAgents、AI Hedge Fund —— 用 LLM 角色扮演/辩论直接给出买/卖/持有决策。
- **量化引擎/工具层**：Kronos（K 线基础模型）、Qbot（全链路机器人）、QuantDinger（自托管 AI 交易 OS）、zhengxi-views（可溯源基金经理观点 Skill）。

本项目定位在**"证据—论点—组合—执行—复盘"的审计闭环**：LLM 只做研究结论，仓位/风控/成本/订单由确定性程序接管，输入侧强制 PIT，每个对象带 `trace_id` 可离线回放。这七个邻居中，没有任何一个把上述六件事同时做到项目级完成度——它们各自解决了其中一到两个子问题。因此本文档不做"谁更强"的横向排名（本提案的原则之一），只做**定位差异**、**本项目解决的具体问题**与**可借鉴之处**三段说明。

---

## 1. 定位对比矩阵

| 维度 | 本项目 | TradingAgents | AI Hedge Fund | OpenBB | Kronos | Qbot | QuantDinger | zhengxi-views |
|---|---|---|---|---|---|---|---|---|
| 主要目标 | 可审计的多 Agent 研究 + 模拟执行 | 模拟交易公司的多角色决策 | 18+ 投资大师人设的选股/择时 | 统一金融数据接口 + 终端 | K 线基础模型（tokenizer + AR Transformer） | 数据→回测→实盘全链路机器人 | 自托管 AI 交易 OS | 单基金经理观点可溯源问答 |
| LLM 角色 | 只出 ClaimCard / PortfolioIntent | 直接给最终交易决策 | PM 给交易决策 | Copilot 辅助查询（可选） | 不涉及 LLM | 不涉及 LLM（ML/RL） | AI 研究助手 + Python 策略 | 语料问答与打分 |
| 时点安全（PIT） | ✅ Snapshot + `as_of` 严格校验；未来数据显式拒绝 | ❌ 拉的是"当下"数据 | ❌ 依赖 Financial Datasets 快照 | ⚠️ 有 `end_date` 但无契约级 enforcement | ⚠️ 输入序列本身是历史 | ❌ 传统回测框架层面处理 | ⚠️ 回测有、研究层无 | ✅ 每段引用带年份/出处 |
| 证据引用（citations） | ✅ `EvidenceCard` → `ClaimCard.supporting_evidence_ids`，契约层强制 | ❌ 自然语言报告 | ❌ 人设口吻输出 | ❌ 无研究结论 | ❌ 时序模型 | ❌ | ❌ | ✅ 引用原文段落 |
| 组合权重生成 | 确定性 `IntentConstraintBuilder`→`RiskBudgetAllocator`→`VolatilityTarget` | LLM 直接给建议 | Portfolio Manager LLM 给决策 | 不提供 | 不提供 | 策略文件硬编码 | Python 策略代码 | 不提供 |
| 硬风控 + 成本门控 | ✅ `RiskProjection` 只减不增；`CostGate` 逐订单 notional 估算 | ⚠️ Risk Management 是 LLM 讨论 | ⚠️ Risk Manager 计算指标但不锁订单 | ❌ | ❌ | ⚠️ 依赖 backtrader/vnpy | ✅ 有回测/实盘执行层 | ❌ |
| 拒答 / 弃权路径 | ✅ 显式 `abstain`/`data_request`/`human_review` 路由 + 原因码 | ❌ 一定给方向 | ❌ 一定给方向 | N/A | N/A | N/A | ⚠️ | ✅ "查无此言"就说查无此言 |
| 全链路回放 | ✅ `trace_id` 串接所有对象（Snapshot/Card/Intent/Order/Fill/Outcome） | ❌ 只保留最终报告 | ❌ | ❌ | ❌ | ❌ | ⚠️ 数据库有历史 | ❌ |
| 执行环境 | T-1 决策 + T 日开盘模拟撮合；不接入实盘 | 无（决策报告） | 无 | 无 | 无 | 支持实盘（vnpy/easytrader） | 加密货币/美股/外汇实盘 | 无 |
| 结果复盘 | ✅ `OutcomeGraph` + 反事实 `NoTrade` OutcomeCard | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| 开源协议 | 私有 | Apache-2.0 | MIT | AGPLv3（部分核心）| MIT | MIT | Apache-2.0 | MIT |
| GitHub Stars（量级） | — | ~93k | ~62k | ~70k | ~30k+ | ~17k | ~10k | ~1k |

图例：✅ 明确实现 / ⚠️ 部分覆盖 / ❌ 未涉及 / N/A 不在项目范围

---

## 2. 逐项目对比

### 2.1 TradingAgents（TauricResearch）

**它做什么**：论文 arXiv:2412.20138（AAAI 2025）的开源实现，用 LangGraph 编排一家"AI 交易公司"：Fundamentals / Sentiment / News / Technicals 四个 Analyst → Bull / Bear Researcher 辩论 → Trader 出决策 → Risk Management 审查 → Portfolio Manager 放行。v0.4.0 支持 GPT-5.x / Gemini 3.x / Claude 4.x / Grok 4.x 多后端。

**核心差异**：

1. **决策输出形态**：TradingAgents 最终输出"自然语言交易报告 + buy/sell/hold 标签"；本项目最终输出是 `PortfolioIntent`——每只标的只有 `long/hold/reduce/exit/abstain` 五档动作 + 强度 + 优先级 + 引用的 Card ID，**契约层不存在权重字段**（`contracts.py:216`）。权重由 [RiskBudgetAllocator](file:///d:/Codes/Trader/core/portfolio/policy.py) 用逆波动率 + 协方差惩罚 + 换手惩罚的 QP 求解，不由 LLM 给。
2. **证据归属**：TradingAgents 的报告里"我引用了哪篇新闻"是散文化叙述；本项目每条 `ClaimCard` 必须携带 `supporting_evidence_ids`，`contracts.py:166` 的 `require_citation` validator 直接拒绝空引用的 bullish/bearish Card。
3. **Bull/Bear 辩论 vs. Critic 扇入**：TradingAgents 用两个 Agent 对辩；本项目 `PerAssetResearchGraph` 是 `(Event→Fundamental) ‖ Market → Risk Critic → ResearchPacket`——Event/Fundamental/Market 三条独立立论，Critic 做**软否决**（allow/caution/abstain/human_review），把冲突与时效问题写入 verdict 而不是再辩一轮。辩论会诱导 LLM 生产更戏剧化的观点；扇入式 Critic 更接近审计要求。
4. **数据可得时间**：TradingAgents 从 finnhub / yfinance 拉的是"当下"数据，`news_analyst` 拿到的新闻没有 `published_at` vs `available_at` 分离；本项目 `EvidenceCard.available_at` 与 `published_at` 双字段，PIT 校验在 Gateway 拒绝任何 `available_at > snapshot.as_of` 的卡片。
5. **执行**：TradingAgents 到 Portfolio Manager 就结束；本项目继续走 `SimulatedExecution` 做 T-1/T 日撮合，`Fill` 契约强制 `executed_at > decision_at`。

**可借鉴之处**：
- Bull / Bear 的**对抗性 prompt 结构**可以嫁接到 Risk Critic 之前——让一个"red-team" Agent 专门对 Event ClaimCard 构造反证，产出 `contradicting_evidence_ids`（本项目 schema 已预留此字段但未强制生成）。
- 多 LLM 后端热插拔的工程实现（本项目 `LLMClient` 目前主要 deepseek + openai 兼容路径）。

### 2.2 AI Hedge Fund（virattt）

**它做什么**：18–19 位"投资大师人设" Agent（Buffett、Munger、Cathie Wood、Soros、Graham、Damodaran、Taleb…），每位用独特 prompt 分析同一批标的，加信号类 Agent（Valuation / Sentiment / Fundamentals / Technicals）与决策层（Risk Manager + Portfolio Manager）。LangGraph 编排、Financial Datasets API 供数据、CLI 与 Web 双前端。

**核心差异**：

1. **人设驱动 vs. 证据驱动**：AI Hedge Fund 的观点来自"如果巴菲特会怎么说"——本质是 LLM 的风格化输出，同一份财务数据下 18 位大师给 18 个理由，但每个理由都不引用具体证据 ID。本项目按**研究维度分工**（Event/Fundamental/Market/Critic），不做**风格维度分工**，每个维度必须带 `EvidenceCard` 引用。
2. **可复现性**：AI Hedge Fund README 明确写"同一 ticker 两次运行结果可能不同"（LLM 非确定性）；本项目通过**冻结 Snapshot + 只读 Gateway + 无状态 Agent**保证同一份 Snapshot 重放得到同一批输入（LLM 输出可能变，但输入完全可控，`trace_id` 记录模型/提示词版本）。
3. **仓位与订单**：AI Hedge Fund 的 Portfolio Manager 直接输出目标持仓百分比；本项目 PM 等价物（Committee）在 [_validate_items](file:///d:/Codes/Trader/agents/research/committee.py) 中做**分级违规治理**：标的局部违规只 abstain 单只、全局结构违规全池 abstain、任何矫正都把模型原始响应以 `tool_calls.raw_response` 记账——保证"模型想说什么"和"系统最终采纳什么"都可回放。
4. **回测口径**：AI Hedge Fund 的回测基于日线快照；本项目 SimulatedExecution 走 T-1 决策 / T 日开盘 + 佣金 + 半价差滑点，与旧 [backtest_engine.py](file:///d:/Codes/Trader/core/backtest_engine.py) 完全隔离。

**可借鉴之处**：
- **风格 personas 作为消融对照组**：可以在本项目契约不变的前提下，把 Committee 的 prompt 换成"Buffett 视角下的 ThesisBook 阅读"，作为**风格 vs. 证据**的 ablation arm，用来量化"人设多样性"是否比"证据维度分工"贡献更多 Rank IC。
- **CLI + Web 双入口**：本项目当前 `scripts/research_run.py` 只有 CLI + [viz/](file:///d:/Codes/Trader/viz/server.py) 观测台，可以借鉴 ai-hedge-fund 把 `--selected-analysts` 类参数暴露给前端做逐 Agent 开关。

### 2.3 OpenBB（OpenBB-finance）

**它做什么**：不是决策系统，是**金融数据适配器 + 分析工作空间**。统一 350+ 数据源、300k+ 符号，Provider Layer / Data Layer / Interface Layer 三层架构；终端 → SDK → Platform → Workspace → Copilot + Agents 一路演进；AGPLv3 开源。

**核心差异**（其实是**互补**）：

1. **职责边界**：OpenBB 停在 DataFrame；本项目从 DataFrame 起步继续做 EvidenceCard / Snapshot / ClaimCard / PortfolioIntent / RiskProjection / CostGate / Fill。OpenBB 完全可以作为本项目的一个 Provider 后端。
2. **PIT 强度**：OpenBB 有 `start_date/end_date` 但只在参数层，没有"这条数据在 as_of 时是否可得"的契约级检查；本项目 `DataGateway.evidence_for_symbol` 强制 `available_at <= snapshot.as_of`，Snapshot 里任何一张违规 Card 直接抛错。
3. **Provider 抽象**：OpenBB 的 Provider 系统（`openbb_core.app.provider_interface`）比本项目 [core/data/](file:///d:/Codes/Trader/core/data/) 更成熟——本项目已有 `NewsProvider / FilingProvider / MarketDataProvider` 概念但未做成插件式注册。

**可借鉴之处**：
- **Provider 注册与 schema 校验模式**：OpenBB 用 pydantic model + `QOpenBBMessage` 做统一的 Provider 描述与响应校验，值得本项目 [core/data/](file:///d:/Codes/Trader/core/data/) 借鉴。
- **LLM Friendly Mode**：OpenBB 2025 年推出专门给 LLM 精简的响应格式（去掉多余字段、返回 markdown 表格），对本项目注入 hook 的 token 预算控制直接可用。
- **数据源切换的 UX**：本项目目前 `yfinance / Alpha Vantage / SEC EDGAR` 是硬绑定，OpenBB 的 provider 字符串切换值得参考。

### 2.4 Kronos（shiyu-coder）

**它做什么**：AAAI 2026 论文 *"Kronos: A Foundation Model for the Language of Financial Markets"*。全球首个开源 K 线基础模型：BSQ（Binary Spherical Quantization）Tokenizer 把 OHLCV 编码为层次化离散 token（coarse + fine subtoken），decoder-only Transformer 在 45 个交易所、120 亿条 K 线上预训练；零样本 RankIC 比最强通用 TSFM 提升 93%。模型规格 mini 4.1M / small 24.7M / base 102.3M 已开源。

**核心差异**（**完全正交**）：

1. **抽象层次**：Kronos 是**输入表征层**——它把 K 线变成 token；本项目是**决策链路层**——它把新闻、披露、行情变成 EvidenceCard 与研究结论。Kronos 的输出（预测的下一段 K 线路径）可以喂给本项目的 Market Agent 作为 market EvidenceCard 的一个字段。
2. **概率预测 vs. 单点判断**：Kronos 通过多路径采样给分布预测；本项目 Market Agent 目前是程序算 1/5/20 日收益 + 波动率 + 回撤的**确定性事实**给模型读。可以把 Kronos 采样分布作为额外事实卡（"K 线未来 20 日路径的 5% / 50% / 95% 分位"）。
3. **不做交易决策**：Kronos 明确不接实盘；本项目也不做实盘但做完整模拟执行 + 复盘。

**可借鉴之处**（最有价值的一层）：
- **Market EvidenceCard 的特征增强**：本项目 [core/data/market.py](file:///d:/Codes/Trader/core/data/market.py) 中的 `build_feature_card` 目前是手工特征（收益率 / 波动率 / 成交量异常 / 回撤 / 趋势）；可以把 Kronos-small/base 的 latent embedding 作为额外字段序列化进 `body` JSON，让 Market Agent 拿到"预训练时序模型已经消化过的市场状态"。这**不改变任何契约**——Gateway 只是多返回几个字段。
- **Tokenizer 思路的迁移**：Kronos 用 coarse/fine 分离市场结构（regime）与噪声（intraday）；本项目 regime 目前是外部传入的字符串标签，可以借鉴分层思想做一个 regime 卡片（coarse 层）+ 微观异常卡片（fine 层）。
- **多路径采样用于风控**：Kronos 的 Monte Carlo 采样可以喂给 [RiskProjection](file:///d:/Codes/Trader/core/risk_manager.py) 做尾部风险估计，比当前基于协方差的正态假设更贴近金融数据的重尾特性。

### 2.5 Qbot（UFund-Me）

**它做什么**：AI 自动量化交易机器人。整合 tushare / backtrader / easytrader / qlib / vnpy 等，从数据获取 → 因子选股 → 机器学习/强化学习策略 → 回测 → 模拟盘 → 实盘的完整闭环；GUI（Web + 桌面 + 移动）；邮件/飞书/微信/弹窗多通道提醒。17k+ Stars，MIT。

**核心差异**：

1. **技术栈世代差**：Qbot 是 2022–2023 年"ML + 多因子"时代的产物，策略文件是 Python 函数；本项目是 2025+ "LLM Agent + 契约审计"时代的产物，研究结论是 Pydantic 对象。
2. **可解释性来源**：Qbot 的解释靠 quantstats 报告 + 因子暴露；本项目的解释靠 `trace_id` 回放到当时冻结的 Snapshot、看到的 EvidenceCard 与生成的 ClaimCard。
3. **实盘范围**：Qbot 支持股票/基金/期货/数字货币实盘；本项目在 Plan.md §1 明确"未经明确授权不接入实盘自动下单"，只做 T-1/T 日模拟撮合。
4. **多市场覆盖**：Qbot 主打 A 股 + 加密；本项目首版限定美股（SEC EDGAR + yfinance + Alpha Vantage）。

**可借鉴之处**：
- **多通道提醒**：本项目 `logs/` 目前只落文件，可以借鉴 Qbot 的邮件 / 飞书 webhook 集成，作为 `decide` 模式产出 PortfolioIntent 后的推送机制。
- **策略注册的插件化**：Qbot 的 `pyfunds` / `pytrader` 分层把"因子选股"和"择时策略"作为独立可插拔模块；本项目的 PPO / 逆波动率 / Top-K 目前散落在 [core/ppo/](file:///d:/Codes/Trader/core/ppo/) 与 [core/portfolio/](file:///d:/Codes/Trader/core/portfolio/)，可以按同样思路整理成 `strategy registry`。
- **本地部署脚本**：Qbot 的 `env_setup.sh` + 一键 `python main.py` 值得参考，本项目当前依赖 conda trader 环境 + 多个脚本手动串。

### 2.6 QuantDinger（OpenByteInc / brokermr810）

**它做什么**：Apache-2.0 自托管 AI 交易 OS；Docker Compose 一键部署；AI 市场研究 + Strategy API V2 Python 策略 + 服务器端回测 + 加密货币（Binance/OKX/Bybit）/美股（IBKR/Alpaca）/外汇（MT5）模拟与实盘；PC Web 8888 + 移动 H5 8889 + API 5000；MCP 接入支持外部 Agent 调用。

**核心差异**：

1. **产品定位**：QuantDinger 是**面向独立交易者的成品平台**（"你的服务器、你的数据"），LLM 是研究助手，策略仍是 Python 代码；本项目是**面向研究/教学/审计的多 Agent 系统**，LLM 直接产出研究结论，代码只做确定性后处理。
2. **策略层 vs. 研究层**：QuantDinger 的"AI 研究"是行情感知 + 置信度评估，最终仍需人写 Python 策略；本项目的 Committee 直接产出 `PortfolioIntent`，中间没有人工。
3. **成本模型**：QuantDinger 通过交易所真实报价；本项目 [CostGate](file:///d:/Codes/Trader/core/portfolio/cost_gate.py) 用佣金 + 半价差滑点 + 冲击模型估算——两种成本口径可以在实验设计中作为对照。
4. **可复现性**：QuantDinger 提供数据库持久化但没强调离线重放；本项目 `trace_id` 是**架构第一原则**（Plan.md §2.5）。

**可借鉴之处**：
- **Docker Compose 一键部署**：本项目目前只有 conda 环境；提供 Dockerfile + compose 会大幅降低他人复现成本，符合 README 随功能同步的原则。
- **MCP Server 化**：QuantDinger 已支持外部 AI Agent 通过 MCP 调用它的行情/回测；本项目 [viz/server.py](file:///d:/Codes/Trader/viz/server.py) 已经起了 HTTP + SSE 服务的头，可以进一步封装成 MCP Server，把 Gateway 的只读接口对外暴露（严格保持 Gateway 只读、拒绝写操作的边界）。
- **多后端 LLM 路由（LiteLLM）**：QuantDinger 用 LiteLLM 做多 Provider 转发；本项目 [utils/llm_client.py](file:///d:/Codes/Trader/utils/llm_client.py) 目前手写 OpenAI 兼容路径，LiteLLM 可以省掉这块维护成本。

### 2.7 zhengxi-views（lyra81604）

**它做什么**：Agent Skill 规范的开源实现，把易方达基金经理郑希 2012–2026 年**全部公开观点**（定期报告、经理手记、媒体采访）建成语料库，配一份从语料蒸馏的"投资方法框架"（每条都有郑希原话佐证），加上全市场 2.7 万只基金数据 + 郑希 8 只基金季度快照。核心约束："**不杜撰**：语料有就引原文，没有就明确标注'按他的方法推演'"。产出溯源问答、观点演变、六维评分卡。

**核心差异**（**哲学最接近本项目，范围最窄**）：

1. **共同点**：两者都把"引用可追溯 + 不编造"当作第一原则。zhengxi-views 的"每句话都能追溯到哪一年哪一篇原话"，与本项目 `ClaimCard.supporting_evidence_ids` + `EvidenceCard.content_hash` + `RawReference.sha256` 是同一件事的两种实现。
2. **静态语料 vs. 动态 Snapshot**：zhengxi-views 的语料是人工整理的历史 PDF/网页，一次入库长期复用；本项目 `SnapshotBuilder.ingest_records` + `freeze_snapshot` 是每日/事件驱动的动态入池 + 每轮决策冻结。
3. **无组合层**：zhengxi-views 只出问答与打分卡，不接仓位、风控、订单；本项目 Committee 之后的四段确定性链路（Intent → Target → Projected → Order → Fill）是它完全没有的。
4. **无 PIT 概念**：zhengxi-views 的语料本身是"历史上某年某人说过什么"，天然时间安全；本项目要处理的是"as_of 时刻市场对某新闻已经消化了多少"，需要显式 `available_at`。

**可借鉴之处**（最贴合本项目气质）：
- **"六维评分卡"的输出结构**：zhengxi-views 让每只基金得到六个维度的分数 + 每维度一句话依据 + 总分/评级/是否契合。这可以直接映射为本项目 `ResearchPacket` 的**摘要视图**——把当前只包含 `critic.verdict + citation_coverage + conflicts` 的 Packet，扩展为"事件维 / 基本面维 / 行情维 / 流动性维 / 组合冲突维 / 时效维"六维打分表，让 Committee prompt 更容易对齐。
- **"语料未覆盖 → 首句加粗声明非本人观点"** 的话术模板：本项目 abstain 路由目前用 reason_code 结构化，但可以在 prompt 层强制一句"以下推演不基于本轮 Snapshot 直接引用的证据"作为话术护栏，避免模型把 `data_request` 之后的补查内容混入正式 ClaimCard。
- **单标的深度 vs. 全池广度**：zhengxi-views 只做郑希一人观点但极深；本项目当前 10 只股票池 × 每标的多张 Card 已经比较宽；未来若要扩池到 100+，可以参考 zhengxi-views 的**渐进式加载**（`references/corpus_index.json` 先给索引，按需展开段落）来控 token。

---

## 3. 本项目解决了什么（提案语气的差异化贡献）

对照上述七个邻居，本项目的**具体增量**是六件事的组合，任何单一邻居都没有同时具备：

### 3.1 时点安全从"约定"升级为"契约"

OpenBB 有 `end_date` 参数、TradingAgents / AI Hedge Fund 有日期字符串、Kronos 有历史窗口——但没有一个在**数据对象层**强制 `available_at` 与 `published_at` 分离，并在读取时抛错。本项目 `EvidenceCard.available_at` 是 pydantic 必填字段（`contracts.py:68`），`DataGateway.evidence_for_symbol` 遇到 `card.available_at > snapshot.as_of` 直接抛错（`gateway.py:49`）。PIT 违规数被 [Plan.md §7](file:///d:/Codes/Trader/docs/Plan.md) 列为强制报告指标。

### 3.2 研究结论从"自然语言报告"升级为"带引用的可校验对象"

TradingAgents 与 AI Hedge Fund 的最终输出是不可结构化的散文；本项目 `ClaimCard` 契约层强制 `supporting_evidence_ids` 非空（bullish/bearish 时），`ResearchPacket.citation_coverage` 是可量化字段，`PortfolioIntent.items[].supporting_card_ids` 是引用链的最后一公里。**引用覆盖率、schema 合规率、冲突率**都可以直接从 trace 算出来，不需要人评。

### 3.3 弃权是一等公民而非 fallback

七个邻居里只有 zhengxi-views 显式实现了"查无此言就说查无此言"。本项目 `NodeRoute` 有六条：`proceed / abstain / data_request / human_review / no_trade / failed`（`contracts.py:106`），任何非 `proceed` 路由必须携带 `ReasonCode`——把"拒答"做成可审计对象，而不是 prompt 里"如果不确定就说不知道"的软约束。三层 abstain（Event/Fundamental/Market 单节点 abstain → Critic abstain → Committee abstain）通过 `ResearchPacket` 逐层传导。

### 3.4 LLM 与仓位生成物理隔离

TradingAgents 与 AI Hedge Fund 的 PM 让 LLM 直接给百分比；QuantDinger 让人写 Python 但 AI 不参与仓位；Qbot 完全没 LLM。本项目的物理隔离在契约层：`PortfolioIntent` `extra="forbid"` + 字段列表里**根本不存在 weight/expected_return/order 字段**（`contracts.py:213-228`）；LLM 想越界唯一的路径是抛 pydantic 错并被记录成违规 trace，无法把非法字段带进下游。之后 [IntentConstraintBuilder](file:///d:/Codes/Trader/core/portfolio/policy.py) 把动作翻译为 `WeightConstraint`，[RiskBudgetAllocator](file:///d:/Codes/Trader/core/portfolio/policy.py) 用 cvxpy 求解，全程无 LLM。

### 3.5 硬风控 + 成本门控作为独立阶段

TradingAgents 的 Risk Management 是 LLM 讨论；AI Hedge Fund 的 Risk Manager 计算指标但不锁订单。本项目 `RiskProjection` 契约级"只减不增"（`contracts.py:293`：`RiskAdjustment` validator 强制 `to_weight <= from_weight`），`CostGate` 逐订单 notional 估算佣金/半价差/冲击并输出 `DeferredTrade` 集合（`contracts.py:325`）——**风控可以否决目标但永远不能新增风险，成本可以延后但不会假装成交**。

### 3.6 端到端 trace_id 回放

七个邻居中**没有一个**声称能对一次决策做完整回放。本项目 `Contract` 基类必带 `trace_id + schema_version + created_at`（`contracts.py:41-43`），从 Snapshot → EvidenceCard → ClaimCard → ResearchPacket → ThesisBook → PortfolioIntent → IntentConstraints → TargetPortfolio → RiskProjectedPortfolio → OrderPlan → Fill → OutcomeCard 每一环写入 `data/research/ledger.db`；[Plan.md §4.3](file:///d:/Codes/Trader/docs/Plan.md) 定"缺失任一关键对象即标记回放失败"。这是**评测设计与消融实验**能落地的物理前提。

### 3.7 反静默篡改：违规治理的分级

TradingAgents / AI Hedge Fund 若 LLM 输出非法字段，通常整次调用失败或忽略；本项目 Committee 的 `_validate_items` 分三级：
- **标的局部违规**：只把该 item 替换为 abstain，其他合规 item 保留；
- **全局结构违规**（symbol 集合缺失/重复/越池）：全池 abstain，不部分采信；
- **反静默篡改**：任何矫正都把模型原始响应以 `tool_calls.raw_response` 记账，保证"模型想说什么"和"系统采纳了什么"分开可查。

---

## 4. 从七个邻居可借鉴的清单（按优先级）

### 高优先（架构级或能显著降低现有实现成本）

| 借鉴点 | 来源 | 落地位置候选 | 理由 |
|---|---|---|---|
| Kronos embedding 作为 market EvidenceCard 补充字段 | Kronos | [core/data/market.py](file:///d:/Codes/Trader/core/data/market.py) `build_feature_card` | 不改契约，纯增强输入侧信息密度；对比手工特征的 Rank IC 提升可直接消融 |
| Provider 注册 + schema 校验（pydantic + 字符串切换） | OpenBB | [core/data/](file:///d:/Codes/Trader/core/data/) 抽 `ProviderRegistry` | 现有 yfinance / AV / SEC 三源硬编码，切换与新增成本高 |
| LiteLLM 后端多模型路由 | QuantDinger | [utils/llm_client.py](file:///d:/Codes/Trader/utils/llm_client.py) | 手写 OpenAI 兼容路径难以覆盖 Claude/Gemini/Grok |
| Docker Compose 一键部署 | QuantDinger / Qbot | `Dockerfile` + `docker-compose.yml`（新增） | 学术复现的门槛正在成为论文评审扣分项 |

### 中优先（能改善现有 UX 或研究深度）

| 借鉴点 | 来源 | 落地位置候选 | 理由 |
|---|---|---|---|
| 六维评分卡作为 `ResearchPacket` 摘要视图 | zhengxi-views | [viz/readmodel.py](file:///d:/Codes/Trader/viz/readmodel.py) | Packet 目前字段较扁平，摘要视图能提升 Committee prompt 与人工 review 效率 |
| Bull/Bear 对抗式 red-team 生成 `contradicting_evidence_ids` | TradingAgents | [agents/research/](file:///d:/Codes/Trader/agents/research/) 新增 red-team hook（不新增 Agent 文件） | 现有 schema 已预留字段但基本为空，加入对抗式补证 |
| 多通道推送（邮件 / 飞书 webhook） | Qbot | [scripts/research_run.py](file:///d:/Codes/Trader/scripts/research_run.py) 决策完成后钩子 | 目前只落 logs，缺实时通知 |
| MCP Server 化 Gateway 只读接口 | QuantDinger | [viz/server.py](file:///d:/Codes/Trader/viz/server.py) 增加 MCP 端点 | 让外部 Agent（Claude Desktop / Cursor）能只读消费本项目证据 |

### 低优先（锦上添花或需先明确收益）

| 借鉴点 | 来源 | 说明 |
|---|---|---|
| 投资大师 persona 作为消融对照组 | AI Hedge Fund | 需要额外一批 LLM 调用，仅在论文有"人设多样性 vs. 证据维度分工"对照需求时启用 |
| 渐进式加载（索引 + 按需展开段落） | zhengxi-views | 当前 10 只股票池 token 未吃紧；扩池到 100+ 时再引入 |
| 实盘券商 connector（IBKR / Alpaca） | QuantDinger / Qbot | 明确违反 Plan.md §1"未授权不接实盘"；**不做** |

### 明确不采纳

- **让 LLM 直接给仓位百分比**（TradingAgents / AI Hedge Fund）——破坏 §3.4 的物理隔离。
- **Bull vs. Bear 辩论结构替代 Critic**（TradingAgents）——戏剧化输出会污染 `citation_coverage`；red-team 只做旁证，不改主链。
- **GUI-first 产品化**（Qbot / QuantDinger 的 Web + 移动）——本项目定位是研究系统，[viz/](file:///d:/Codes/Trader/viz/server.py) 只服务单人调试。

---

## 5. 一句话总结每个邻居与本项目的关系

- **TradingAgents** —— 最直接的对比对象，同一范式（多 Agent LLM 交易）但本项目多了 PIT、引用契约、拒答路由和确定性组合层。
- **AI Hedge Fund** —— 概念最近但哲学最远：人设驱动 vs. 证据驱动；persona 可作为消融对照而非主线。
- **OpenBB** —— **不是竞品而是底座**：本项目 Provider 抽象的成熟版；可以对接而非重造。
- **Kronos** —— **正交而非重叠**：金融 K 线基础模型作为 Market EvidenceCard 的输入增强层最合理。
- **Qbot** —— 上一代"ML + 多因子 + 实盘"闭环；本项目不做实盘、不主打因子；借鉴提醒与插件注册。
- **QuantDinger** —— 面向独立交易者的成品 OS；本项目不做产品化；借鉴 Docker 与 MCP Server 化。
- **zhengxi-views** —— 气质最接近的"小兄弟"：单标的深度 + 严格溯源；借鉴六维评分卡与语料未覆盖话术。

---

## 6. 引用与元信息

| 项目 | 仓库 | 关键论文 / 文档 | 许可 |
|---|---|---|---|
| TradingAgents | github.com/TauricResearch/TradingAgents | arXiv:2412.20138（AAAI 2025） | Apache-2.0 |
| AI Hedge Fund | github.com/virattt/ai-hedge-fund | README + `src/agents/*.py` | 未声明（默认版权保留）|
| OpenBB | github.com/OpenBB-finance/OpenBB | docs.openbb.co（Platform / Provider 架构） | AGPLv3 |
| Kronos | github.com/shiyu-coder/Kronos | arXiv K-line Foundation Model（AAAI 2026） | MIT |
| Qbot | github.com/UFund-Me/Qbot | ufund-me.github.io/Qbot | MIT |
| QuantDinger | github.com/OpenByteInc/QuantDinger（镜像：brokermr810/QuantDinger） | quantdinger.com/doc | Apache-2.0 |
| zhengxi-views | github.com/lyra81604/zhengxi-views | README + SKILL.md | MIT |

Star 数为 2026-10 观察量级，仅用于反映社区规模，不用于评估技术优劣。

**评估边界声明**：本文档不做"本项目优于以上七者"的断言；只在明确定位差异后指出**本项目的差异化贡献**（§3）与**可借鉴之处**（§4）。任何量化对比需要在 [Plan.md §7](file:///d:/Codes/Trader/docs/Plan.md) 规定的统一 PIT、成本与风险条件下通过 [experiment_index.csv](experiment_index.csv) 登记的消融实验验证后方可写入提案正文。
