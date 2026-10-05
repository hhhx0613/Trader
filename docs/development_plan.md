# 开发实施计划

> 本文件是 [Plan.md](Plan.md) 的实施拆解，只追踪实施进度，不替代架构决策。架构、对象边界和业务规则以 `Plan.md` 为准。
> 组织方式：按每日业务流转顺序分阶段——数据事实先于研究，研究意图先于组合，组合先于订单，客观结果最后进入复盘。**

## 1. 实施总则

1. **一条主线。** `采集 -> PIT 过滤 -> Snapshot -> PerAssetResearch -> Committee -> PortfolioPolicy -> RiskProjection -> CostGate -> SimulatedExecution -> OutcomeGraph`。旧 Top-K、VADER、单 LLM 分数、逆波动率公式和 PPO 只作为独立对照，不与新主线共同决定任何一笔订单。
2. **复用优先的次序。** 先用现有函数完成任务；现有职责不满足 `Plan.md` 的 PIT/审计边界时才改造；只有确认全仓库没有等价实现时才新增。新增前先在本文件第 2 节的资产清单里找不到落点，才允许建目录。
3. **数据和判断分离。** `core/research/` 只保存事实、引用、冻结快照和审计；`agents/research/` 只在冻结输入上形成研究判断；组合、风控、成本和订单是确定性程序，不接收原始 LLM 文本。
4. **骨架不等于完成。** 离线 fixture 通过只说明行为可测；每阶段还需真实小样本验收、记录已知限制并满足阶段门槛，才可进入下一阶段。
5. **失败显式化。** 数据缺失、PIT 无法证明、引用不完整、模型失败、证据冲突和不可交易均须有稳定原因码，落入 `abstain`、`data_request`、`human_review`、`no_trade` 或延后交易分支；不得静默降级。

## 2. 现有代码资产清单（2026-10-03 核实）

| 位置 | 已有能力（可直接调用） | 新主线用法 |
| --- | --- | --- |
| `core/research/contracts.py` | `Contract` 基类（frozen、`schema_version`/`trace_id`/`created_at`）、`RawReference`、`EvidenceCard`、`ResearchSnapshot` | 【改造】补齐其余契约类，复用基类与字段约定 |
| `core/research/store.py` | `ResearchLedger`：WAL、外键、版本化迁移、追加写、trace 完成后只读；**14 张对象表已一次性建好**（含 claim_cards、order_plans、fills 等） | 【复用】；【改造】仅补各对象的 append/get 类型化方法 |
| `core/research/providers.py` | `normalize_record`、`validate_available_at`、`canonical_content_hash`、`PITValidationError`（带原因码） | 【复用】所有 Provider 适配器统一走这里，不再各写一份校验 |
| `core/research/snapshot.py` | `SnapshotBuilder` 双入口：`ingest_records`（PIT 校验 -> 去重 -> 存原始载荷 -> 建卡）与 `freeze_snapshot`（显式卡集或 `select_evidence` 池选卡后冻结），各占独立 trace | 【改造】加最低数据要求检查与拒绝记录；不重写流程 |
| `core/research/gateway.py` | `DataGateway.evidence_for_symbol`：snapshot/symbol/as_of 边界校验 + `record_query` 审计 | 【改造】按 Plan.md 4.2 表扩展查询方法，复用同一校验与审计模式 |
| `core/research/raw.py` | `RawPayloadStore` 内容寻址存储 | 【复用】 |
| `core/data/news.py` | `fetch_news_records`：三源同构记录 + coverage 拉取台账（历史网格段整段/未走完段拆单日）+ AV->Finnhub 降级，正文直落 news.db | 【复用】主线新闻入口 |
| `core/data/market.py` | `ensure_market_coverage`（K 线素材批次）+ `build_feature_card`（每周一张特征卡=事实）+ `load_price_frame`（PIT 价格帧，同日取最早 available_at） | 【复用】主线行情入口 |
| `core/legacy/news_data.py`、`core/legacy/market_data.py` | 旧 CSV 缓存路径 `fetch_news`/`fetch_ohlcv`（PPO/回测管线在用） | 【封存】主线不再回用 |
| `core/data/llm_cache_db.py` | `LLMCacheDB`：LLM 调用缓存 | 【改造】缓存 key 补输入哈希与 prompt 版本，可关联 trace |
| `utils/llm_client.py` | `LLMClient`：多 provider、鉴权、重试、JSON 调用 | 【复用】所有研究 Agent 唯一模型入口 |
| `agents/llm_analyst.py` | `load_prompts`（prompt 目录 + 内容 hash 自动版本化） | 【复用】函数上移或导入使用；旧 `LLMAnalystAgent`、L1 记忆、新闻分数留在对照路径 |
| `agents/stock_selector.py` | `analyze_candidate_pool`（多标的并发分析模式） | 【复用】编排模式参考；Top-K 与分数路径不进入主线 |
| `agents/decision_func.py` | `_stock_daily_vol`、`_position_sizing`（逆波动率计算） | 【改造】迁入 `core/portfolio/` 作 `RiskBudgetAllocator` 参考分配，原公式入口保留作对照 |
| `core/risk_manager.py` | 止损、回撤、成本与原因信息 | 【改造】拆为硬约束输入 + `RiskProjection` 确定性规则 |
| `core/base_engine.py`、`core/multi_stock_engine.py` | 账户、持仓、费用记账、T-1 决策/T 日开盘模拟成交 | 【改造】输入边界改为 `OrderPlan` + `trace_id`，账本逻辑不动 |
| `agents/research/graph.py` | `PerAssetResearchGraph.run`：**单标的**、只读已冻结 Snapshot 的研究图（Event → Fundamental ‖ Market → Critic → Packet） | 【复用】只覆盖链路第四段，多标的循环由上层编排负责 |
| `agents/research/flow.py` | `run_research_round`：采集→建卡→冻结→逐标的研究→Committee 的阶段门控编排，`daily`/`collect`/`decide` 三种节奏 | 【复用】CLI、外部定时调度与 `viz/` 共用唯一入口 |
| `scripts/run_backtest.py` | `--purpose`/`--experiment-id`、manifest、`docs/experiment_index.csv` 登记 | 【复用】；【改造】加新主线入口，旧策略保留独立标识 |
| `core/ppo/` | 总暴露实验路径 | 【复用】仅作对照，不进主线仓位决策 |
| `core/data/filings.py` | EDGAR Provider 已交付：ticker→CIK 映射、submissions/XBRL facts 筛选、acceptance 时刻即 `available_at`；网络备忘缓存写 `data/cache/filings/`（非账本） | 【复用】主线披露入口 |

## 3. 当前状态

- 阶段 1 主线已交付（2026-10-03）：三采集器（`core/data/news.py`/`market.py`/`filings.py`）+ 建卡/冻结双入口 + `select_evidence` 池选卡 + 入口薄壳；离线回归 30 项（`tests/research/`）全绿；NVDA 真实轮已冻结验收（news 1045 条、去重后 891 卡，`snap_223d227a…`）。
- 编排收敛（2026-10-05）：采集/建卡/冻结/研究图/委员会的阶段门控上移为 `agents/research/flow.py` 的 `run_research_round`，原 `scripts/capture_research_snapshot.py`（只到冻结）与 `scripts/trace_research_flow.py`（通到 Committee 的逐节点打印）已删除并合并为 `scripts/research_run.py`；`research_run.py` 只保留「跑全程」一个入口（默认 `collect` + 注入模型），`daily`/`decide` 交给外部定时调度器直接调用 `run_research_round`，不再各自养命令行。`viz/` 也已删掉自带的 `pipeline.py`：观测层（事件总线/SSE/取消/TracingModel）合并进 `viz/server.py`，「跑全程」直接复用 `flow.run_research_round`，与 CLI、定时调度器同一函数；仅保留 flow 没有的「复用已冻结 Snapshot 单阶段调试」节奏直接调用生产原语 `PerAssetResearchGraph`/`Committee`。
- 测试收敛（2026-10-05）：删除 PPO/回测线的 `tests/test_ppo_env.py`、`tests/test_ppo_stage2.py`、`tests/test_risk_manager.py`，`tests/` 下只保留研究链回归（`tests/research/`，当前 52 项）。被删测试覆盖的 `core/ppo/`、`core/risk_manager.py` 实现本身保留，但自此无测试覆盖——它们属于旧回测基线，随该线退役时一并删除，不再单独补测。
- 实盘决策时点后置：入口脚本未传 `--as-of` 时采集完才定 `as_of`，本轮到达数据本轮可用；传 `--as-of` 时点钉死，本轮新拉数据被 PIT 正确拒绝。
- 账户口径的已知敞口：`account_state`（含 `regime`、持仓、限额）由调用方手填 JSON 提供，`freeze_snapshot` 对其不做 PIT 校验，因此 `--as-of` 历史回放会把当下的账户与行情判断带入历史时点。当前用法只在实盘时点决策，暂不加校验；`regime` 也不由系统推断，`DataGateway.get_regime()` 仅回读冻结值。
- 尚未开始：最低数据要求检查与 `rejection_code` 落盘、Gateway 查询扩展（阶段 2 依赖）、事件驱动复核入口。
- 历史欠账（用户决定延后）：`data/research/raw/` 下 957 个文件系统时代正文目录是 9 月旧卡的唯一副本，须先按哈希迁入分库再删；`data/cache/news/` 旧 CSV 网格未入账本，如需历史回放按 `available_at = published_at` 假设口径另行处理并明牌标注。
- 权重与订单主线、执行改造和 OutcomeGraph 尚未开始；研究 Agent、单标的图与 Committee 已交付（见阶段 2/3 条目与 `tests/research/`）。

## 4. 阶段 0：补齐契约与冻结图接口

**业务位置：** 接入真实数据或 Agent 前，先让跨模块对象、失败分支和验收口径稳定。

**状态：已完成（2026-10-03，按用户决定缩小范围）。** 交付：`contracts.py` 只冻结「数据 -> 研究 -> Committee」所需契约（新增 `ClaimCard`、`ResearchPacket`、`ThesisBook`、`PortfolioIntent` 与 `RouteDecision`/`CriticVerdict`/`IntentItem` 值对象；基类强制 `extra="forbid"`，Committee 边界对象无法携带权重/订单字段）；**组合/订单/复盘契约（IntentConstraints 至 ReviewLabel）推迟到阶段 4-6 启动时定义**，避免提前冻结看不懂的空概念，对应账本表已在 v1 建好无需改表结构。新增 `reasons.py` 统一原因码枚举，`providers.py`/`snapshot.py` 的 PIT 码接入共享；`store.py` 迁移 v2 补 `thesis_books` 表并为已冻结对象提供 typed append/get；回归见 `tests/research/test_stage0_contracts.py`。验证：`python -m pytest tests/research -q` 全部通过；真实库 `data/research/research_ledger.db` 已干净迁移至版本 2 且历史 trace 完好。已知限制：投影单调性、Packet 合格性等行为约束留待对应阶段测试固化；Gateway 只读接口扩展属阶段 1。

**工作项**

- 【改造】`contracts.py`：沿用 `Contract` 基类补齐研究链路契约 `ClaimCard`、`ResearchPacket`、`ThesisBook`、`PortfolioIntent`；`IntentConstraints` 至 `ReviewLabel` 推迟到阶段 4-6 启动时定义。不新建第二个契约模块。
- 【改造】`store.py`：为已冻结对象补 typed `append_*/get_*`；表结构复用既有 `OBJECT_TABLES`（v2 迁移补上缺失的 `thesis_books`），禁止泛用表名绕过对象校验。
- 【新增】稳定原因码枚举（`missing_available_at`、`future_data`、`stale_data`、`untradable`、`deferred_cost`、`deferred_liquidity` 等），与 `PITValidationError.code` 同一体系。
- 【新增】图节点路由契约文档化：`abstain`/`data_request`/`human_review`/`no_trade`/失败均有显式分支，写进测试 fixture 而非只有注释。

**验收门槛**

- 所有跨模块对象可 Pydantic 校验，缺关键字段得到确定拒绝原因。
- `Plan.md`、本计划、代码类型名、测试名和账本表名没有同义不同名的并行概念。
- 各阶段契约可在离线状态下导入校验，无需网络。

## 5. 阶段 1：每日采集 -> EvidenceCard -> 冻结 Snapshot

**业务位置：** 决策开始时先获得截至 `as_of` 可得的事实，形成唯一可离线重放的 `ResearchSnapshot`；行情硬风险告警与固定周历在此产生触发信号。

**状态：主线已交付（2026-10-03）。** 三源采集器、建卡/冻结双入口、池选卡与 capture 入口脚本均已落地并完成真实 NVDA 冻结轮；下列工作项中采集适配器、SEC Provider、行情可得时间规则、入口脚本已完成，剩余：最低数据要求检查与 `rejection_code` 落盘、Gateway 查询扩展（可随阶段 2 交付）、历史回填与旧载荷迁移（用户决定延后）。

**工作项**

- 【复用】`fetch_news`、`fetch_ohlcv` 的请求、降级、限流和缓存；适配器不得出现第二套 HTTP 或缓存。
- 【新增】新闻薄适配器：`get_news_at_date` 的 DataFrame 行 -> `normalize_record` 可消费的 dict，补 `published_at`、`available_at`、Provider 元数据；`available_at` 定义为接收时间并有文档口径。
- 【新增】行情可得时间规则：日线 EOD 数据何时可入当轮决策（默认 T+1 可得不算未来），复用宽文件缓存读取。
- 【新增】SEC EDGAR Provider（唯一新增数据源）：公司索引、10-K/10-Q/8-K 正文、XBRL facts、分页与节流；`available_at` 用 acceptance/publication time；记录尝试顺序、成功/失败和空结果。
- 【改造】`SnapshotBuilder`：在现有 build 流程上增加每标的最低可用集检查、被拒记录的 `rejection_code` 落盘；数据不足产出可审计的前置 `abstain` 对象。
- 【改造】`DataGateway`：按 Plan.md 4.2 的查询表扩展 `get_filing_section`、`get_xbrl_facts`、`get_market_slice`、`get_regime`、`get_current_portfolio`、`estimate_turnover_cost` 等只读方法，全部复用现有边界校验 + `record_query` 审计；不向 Agent 暴露 Provider、路径或连接。
- 【新增】采集与决策入口脚本：周度调仓与临时触发两种入口，只调用上述既有函数。（已收敛为 `agents/research/flow.py` + `scripts/research_run.py`：`daily` 每日只建卡入池、`collect` 本轮卡集冻结、`decide` 池选卡冻结并跑研究与委员会；实盘/回放两种时点模式由 `--as-of` 控制）
- 【改造】`llm_cache_db`：为阶段 2 准备可关联 trace 的缓存 key；账本与缓存保持物理分离。

**验收门槛**

- 断网可由账本 + 原始载荷重建同一 Snapshot；篡改哈希、未来数据、无 `available_at`、错误标的、重复转载均被拒绝且有原因码。
- 真实小样本覆盖新闻、行情、SEC 三源并保存单一可人工检查的 trace。
- 每个 `EvidenceCard` 可定位原始载荷、哈希、来源和可得时间。仅离线 fixture 通过不得进入阶段 2。

## 6. 阶段 2：单标的 PerAssetResearch（含每日 Event triage）

**业务位置：** 将已审计事实转为带引用、可反驳、可观望的 `ResearchPacket`；Event triage 同时决定本周队列与临时触发。

**状态：进行中（2026-10-04）。** 本轮交付扩展受审计的 `DataGateway`，并在 `agents/research/` 下引入隔离的单标的图；验收为离线 Gateway/图分支覆盖加 `tests/research` 回归，止步于 `ResearchPacket`，不采集数据、不跑回测。拓扑后续校准为 `(Event → Fundamental) ‖ Market → Critic → Packet`：Event 立论后扇出，Market 不再被 Event 弃权连坐，Fundamental 仅在存在事件论点时核验。

**工作项**

- 【新增】`agents/research/` 目录：节点仅使用 `DataGateway`、上游 Card 和程序组装状态；禁止文件、数据库、HTTP、旧聊天记录。
- 【改造】受控 tool-calling harness：模型通过 `LLMClient` 只调用角色白名单中的**只读**查询。harness 固定标的、窗口、参数/次数/字符预算，并持久化工具参数、来源、失败码和返回的 EvidenceCard ID；模型不能取得 Provider 或连接。
- 【删除】Gateway 的联网补齐能力（`ensure_news`/`ensure_filings`/`ensure_market` 与 `ResearchRun` 生命周期）：`DataGateway` 从此只读账本与原始正文库，不发起网络请求、不建卡。理由：拉数据与拉多少共享 AV 账户级日配额，而配额计数是单次调用的局部变量，闸门拦不住自己；模型拿到的空结果区分不了“没新闻”“被情绪分过滤”“被限流”三种情况，证据集因此不可复现。实盘与历史回放现在走同一条只读路径。缺证据的出路是 `data_request` 弃权，缺口由下一轮采集脚本补齐后重新冻结。
- 【改造】节点输入：行情复用 `core/indicators.py` 的程序计算结果并冻结；财报/XBRL 由程序结构化后经 Gateway 提供；新闻保持 PIT 合格的 EvidenceCard 摘录，按需工具检索，不增加新闻摘要管线。
- 【复用】`LLMClient` 作唯一模型调用；复用 `load_prompts` 的目录约定与版本 hash 机制管理各 Agent prompt；每节点记录模型/prompt 版本、输入对象 ID、缓存命中、token、延迟、schema 校验结果和失败码。
- 【新增】Event Agent triage：读取每日新增去重新闻的标题/摘要/来源，分流 `irrelevant`/`weekly`/`immediate`，理由落账本；周度 Event Agent 基于本周队列产出 ClaimCard。
- 【改造】Fundamental Agent：程序先算同比/环比、指引变化和口径差异再喂给 Agent；披露证据只能来自 EDGAR 原始载荷，新闻不得替代；仅在存在事件论点时核验，无则自行跳过不另起论点。
- 【改造】Market Agent：数值全部由程序计算（复用 `core/indicators.py` 现有指标），Agent 只解释与引用本标的 market 字段 ID；不读取 Event、peer 或 regime，作为独立分支在无新闻/无事件论点时仍产出行情判断，不被 Event 弃权连坐。
- 【改造】Risk Critic：基于三张上游 Card + 账户/成本摘要输出 `allow/caution/abstain/human_review`；明确是研究软否决。
- 【改造】ResearchPacket Builder：汇集 Cards、引用覆盖率、时效、冲突、持仓与 Critic 结论；不合格 Packet 不得进入 Committee。非 `abstain` 的 ClaimCard 强制至少一条当前快照内支持 EvidenceCard，并记录反证、未知项和时效。

**验收门槛**

- 同一冻结快照重放得到相同节点输入和可校验输出链。
- 任一非观望结论可回溯 `ResearchPacket -> ClaimCard -> EvidenceCard -> RawPayload`；删除关键证据后下游失效。
- 先离线图测试，再真实小样本 smoke 与人工审查。
- 真实模型 prompt 必须点明各自允许引用的 EvidenceCard ID 与角色专属的判断边界；schema 非法的响应仍由 Pydantic 拒绝，并转成显式 `abstain`，不得退化为兜底交易动作。
- tool calling 的离线验收覆盖：越权工具、超调用预算、工具返回之外的引用、PIT 拒绝、窗口无卡时不得静默去拉新数据，以及 Critic 对持仓/成本/regime/同业信息的只读查询；不运行真实模型、采集或回测。Packet 一律归属启动时冻结的那份 Snapshot，全池 Packet 共享同一 Snapshot ID 与 `as_of`。

## 7. 阶段 3：跨标的 Committee 与 PortfolioIntent

**业务位置：** 汇总全池 Packet 形成研究层动作意图；此处仍不计算权重和订单。

**工作项**

- 【复用】`analyze_candidate_pool` 的并发编排模式实现 `PerAssetResearch` 批量执行（Event 先行立论；随后 Fundamental（有事件论点才核验）与 Market（独立）并行；Critic 收口）。
- 【新增】`ValidateSnapshot` 图入口校验与 `BuildThesisBook`：只给摘要、组合状态、regime、调仓差异和已失效结论，不给原始全文或 Agent 对话；构建带类型的 `ThesisBook` 时须校验传入的每个 Packet 属于同一冻结 Snapshot，再持久化一份覆盖全标的的 `PortfolioIntent`。
- 【新增】Committee：逐标的输出 `long/hold/reduce/exit/abstain`、强度、优先级、持有期、支持/反对 Card ID 和不交易理由，写入 `PortfolioIntent`。
- 【改造】intent 持久化与失败恢复，复用 `store.py` 追加写与 trace 状态机。

**验收门槛**

- Committee 不绕过 Critic 的 `abstain`/`human_review`，不输出权重、预期收益或订单。
- 相同 `ThesisBook` + 配置得到可比较、可审计的 Intent；每项动作可回溯至 Packet。
- `data_request`/`human_review`/`abstain`/节点失败不会隐式转成买入或维持。

## 8. 阶段 4：确定性组合政策、硬风控与成本

**业务位置：** 动作约束 -> 风险预算参考分配 -> 波动率目标 -> 风险投影 -> 成本门控，全程确定性程序。

**工作项**

- 【改造】迁移 `_stock_daily_vol`/`_position_sizing` 到 `core/portfolio/`，作 `RiskBudgetAllocator` 的逆波动率参考分配；`decision_func` 旧公式入口原样保留作对照。
- 【新增】`IntentConstraintBuilder`：五种动作 -> 候选准入、权重上下限、交易许可、强制退出（按 Plan.md 3.1.4 表格实现，优先级不做收益映射）。
- 【改造】`RiskBudgetAllocator`：在动作边界内求解 `min ‖w−b‖² + 协方差 + 换手` 相对权重；协方差复用 `fetch_ohlcv` 缓存收益面板，不新增行情获取。
- 【新增】`VolatilityTarget`：`E_target = min(E_max, σ_target/σ̂_p)`，加暴露限速、减暴露即时。
- 【改造】`RiskManager` 的止损/回撤/成本规则拆为硬限额输入，新增 `RiskProjection`（只减仓、增现金或 `no_trade`，不新增风险、不覆盖 `exit/reduce`），每步带 `reason_code`。
- 【新增】`CostGate`：强制交易优先，再检查最小订单、no-trade band、现金、佣金、滑点、冲击、参与率和最大换手，产出 `OrderPlan` 或 `DeferredTrade`；成本参数【复用】`risk_manager`/引擎现有成本模型，不另建一套。

**验收门槛**

- 相同 Intent、行情、账户、配置必得相同 TargetPortfolio、投影与订单计划（确定性可断言）。
- `RiskProjection` 与 `CostGate` 从不增加风险、不重新选股、不覆盖强制卖出。
- 每项订单或延后可追溯 Intent -> 约束 -> 目标 -> 投影 -> 成本判断的完整对象链。

## 9. 阶段 5：T-1/T 日模拟执行与离线回放

**业务位置：** 已批准的 `OrderPlan` 交由既有撮合与账本记账；执行引擎不重新解释研究或组合。

**工作项**

- 【改造】`MultiStockBacktestEngine` 输入边界：接收 `OrderPlan` + `trace_id` + 原因码；账户、持仓、费用和 T-1/T 开盘成交实现【复用】不动。
- 【改造】成交、部分成交、未成交、排队、停牌和流动性受限写为可审计 `Fill`/延后记录，复用 `LedgerRecorder`（`core/recorder.py`）扩表而非新建记录器。
- 【改造】`run_backtest.py` 加新主线入口（加载冻结数据与 `OrderPlan`），旧 LLM/VADER/Top-K/PPO 保持独立策略标识；`--purpose`/manifest/登记机制直接复用。
- 【新增】离线 replay：给定 `trace_id` 不访问网络重建 Snapshot -> Cards -> Intent -> Target -> 订单 -> 成交链；缺任一关键对象即标记回放失败。

**验收门槛**

- 决策日/成交日/价格/成本口径满足 T-1/T 时序，无未来行情。
- 新旧路径不共享同一订单决定权；旧策略可独立运行作对照。
- 任一成交可定位到 `OrderPlan` 与完整上游 trace。

## 10. 阶段 6：OutcomeGraph、受控记忆与实验治理

**业务位置：** 结果到期后写入不可修改的客观结果，复盘产出标签；以统一协议验证增量。

**工作项**

- 【新增】`OutcomeGraph`：第 1/5/20 交易日、退出或 thesis 失效时生成 `OutcomeCard`；`NoTrade` 生成反事实 Outcome。收益/成本/回撤数值【复用】引擎账本与 `market_data` 计算，复盘调度由外部脚本承担，不引入新调度框架。
- 【改造】Review Agent：复用阶段 2 的 Agent 边界与 `LLMClient`，只读原始证据、Intent、成交和 Outcome，标注有效性或失败模式；不得以 LLM 自评替代客观结果。
- 【新增】受控案例检索：仅保存/返回证据完整、已到期、已复盘、带 trace 的少量案例；未到期不得入长期记忆。旧 L1 记忆（`llm_analyst`）不迁移。
- 【复用】`experiment_index.csv` 登记协议：固定 PIT 截止、股票池、调仓日历、成本模型、风险限额、基线与成功/失败标准。
- 【改造】依次进行研究层消融（无 LLM / 单 LLM / 无 Critic / 完整多 Agent）与组合层对照（等权、逆波动率、约束风险预算、PPO 多种子 + 滚动样本外 + block bootstrap）。

**验收门槛**

- 完整 trace 可离线回放；未到期或无来源摘要不可被检索。
- 同时报告审计指标（引用有效率、冲突率、观望率、回放完整率、延迟、成本）与经济指标（收益、Sharpe、回撤、暴露、换手、成本、Rank IC）。
- 单一短窗口、单次运行或未过 PIT/成本/风险一致性验证的结果只能标为诊断性，不得作 alpha 结论。

## 11. 阶段交接与文档纪律

- 每个阶段结束时，只在本文件对应阶段更新状态、日期、提交或分支、验证命令与结果、审计样本、已知限制和下一阶段前置条件；不新增平行计划文件，不在文末追加无归属待办。
- 数据契约、账本结构、节点边界、组合接口变更时，先更新 `Plan.md`，再改代码和本计划。
- 功能、运行方式、依赖、输出或目录结构变化时同步 `README.md`。
- 回测和实验必须带 `--purpose`、manifest 和登记；不自动生成额外 Markdown 报告。
- 未满足阶段门槛时状态保持"进行中"或"阻塞"，不得提前标记完成或启动依赖其结果的实验。
- 任何工作项落地前先在清单中找落点：能在【复用】【改造】解决的不得【新增】；发现重复实现即视为缺陷优先消除。
