# 量化交易 LLM 多Agent 系统 — 毕设落地计划

## 一、系统概述

**LLM 决定"买哪几只"，PPO 决定"总仓位下多重注"，规则决定"准不准下"，程序忠实执行。**

- 定位：以 LLM 多 Agent 应用为核心，PPO 强化学习作为仓位融合器子模块，规则作为安全护栏
- 目标：方法科学、评估严谨、结论诚实；多 Agent 应用作为核心亮点
- 硬件：普通笔记本、纯 CPU 可跑

### 架构

```
          ┌─ 选哪几只 (Top-K) ─┐   ┌─ 总仓位 (暴露) ─┐   ┌─ 否决权 ─┐
新闻/财报 → [LLM Analyst Agents] → 排序打分 → [PPO 融合器] → 仓位档位 → [规则风控] → 最终持仓 → [程序执行]
技术指标  ─────────────────────────────────↑
市场环境  ─────────────────────────────────↑
账户状态  ─────────────────────────────────↑
历史分析  ─────────────────────────────────↑
```

### 决策流程

1. **LLM Analyst Agents**：读取每只股的新闻/财报 + 历史分析上下文（L1 记忆）→ 输出结构化观点（方向、置信度、理由）→ 对候选池排序，选出 Top-K（默认 K=5）
2. **置信度校准层**：用历史 `(LLM自报confidence → 实际胜率)` 映射表校准 LLM 输出，修正系统性偏差
3. **PPO 融合器**：输入 = 校准后 LLM 观点 + 技术数值 + 市场环境特征 + 账户状态 → 输出总仓位档位（离散：0%/25%/50%/75%/100%）
4. **规则风控**：只缩减仓位或否决，不凭空加仓；含单股上限、总回撤上限、换手惩罚
5. **程序执行**：不做判断，忠实下单并记录

### 核心设计决策

| 决策 | 方案 | 理由 |
|------|------|------|
| 选股 | Top-K（默认 K=5）按 `composite_score × confidence` 排序 + 每周调仓 + 持有优先 | composite_score 编码方向+强度，confidence 编码证据质量；乘积 = 期望信号强度，同时要求方向明确且证据可靠 |
| 仓位 | 波动率目标定总暴露 + 逆波动率定个股权重（阶段 3 公式版）/ PPO 仅做总仓位暴露（阶段 5）；**PPO 日频决策，LLM 周频选股** | **波动率目标+逆波动率加权是业界标准风险平价近似，优于简单等权；PPO 解耦后各展所长** |
| 技术面 | 不用 LLM，交给量化指标 + PPO | 数值推理弱、不可复现、成本高 |
| 牛熊适配 | 单 PPO + regime 特征，不训练多个 PPO | 避免数据切碎和未来函数 |
| 数据缺失 | 返回空 DataFrame 或抛错，禁止模拟数据兜底 | 保证实验真实性 |
| Agent 记忆 | L1 分析上下文 + L-Calibrate 置信度校准（轻量） | 让 LLM 记住上次判断 + 校准系统性偏差，后续可扩展 |

---

## 二、分阶段落地

### 阶段 0 · 现状盘点 + 决策接口预留（0.5–1 周）✅

盘点已有模块并冻结：`market_data.py`（三层兜底 + 缓存）、`indicators.py`、`recorder.py`、`strategy.py`（规则基线）。在 `backtest_engine.py` 中预留外部决策回调接口 `decide(date, market_state, candidate_pool, news_df, current_holdings, market_data) → {holdings: [{symbol, weight}], exposure: 0~1}`，支持持有 K 只标的（逆波动率加权）与按周调仓。

**交付**：资产清单 + 冻结边界说明；规则基线回测结果留档（对照组）；引擎支持外部决策回调。

---

### 阶段 1 · Point-in-Time 数据管道（1–1.5 周）✅

获取"在 t 时刻真实可得"的新闻文本，杜绝未来函数。

- **新闻数据源**（混合收集）：Finnhub（最近 30 天，免费 60 次/天）→ Alpha Vantage（历史新闻）→ 本地缓存 CSV
- **时点对齐**：t 时刻只能使用发布时间 ≤ t 的文本；新闻用发布时间戳，财报用实际公布日
- **离线预下载**：全历史新闻情绪预拉取，存本地 CSV，避免逐日调 API。缓存分 `complete/`（已完成段，回测用）和 `incomplete/`（未完成段，实盘增量用）两个目录管理
- **禁止模拟数据**：缺失时观点填中性、置信度 0，记录日志

**交付**：按 `(symbol, date)` 可查的本地文本/情绪数据集。

---

### 阶段 2 · LLM Analyst Agent（1.5–2 周）✅

实现真正的 LLM Agent（工具调用 + 结构化输出），而非规则打分。

- **输入**：`(symbol, date)` + 该时点可得的新闻
- **结构化输出**（强制 JSON schema）：

```json
{
  "symbol": "AAPL",
  "direction": "bullish | neutral | bearish",
  "confidence": 0.0-1.0,
  "reasons": ["...", "..."],
  "sources": ["新闻标题/链接"]
}
```

- **可复现性**：固定温度、记录 prompt 版本、缓存 LLM 输出
- **对照基线**：实现 `decide_formula_vader` 完整 VADER 决策路径（VADER 情绪 → Top-K 选股 + 波动率目标仓位），作为"LLM vs 规则"消融对照

**交付**：`agents/llm_analyst.py`、`agents/decision_func.py`、`tests/test_stage2.py`。

---

### 阶段 3 · 最简决策链闭环（无 PPO）（1–1.5 周）✅

用公式打通决策链，跑出第一份端到端回测结果。

- **选股**（每周）：LLM 分析候选池 → 按 `composite_score × confidence` 排序返回 Top-K；持有优先
  - `composite_score`（代码从 per_news 确定性计算）：信号方向+强度
  - `confidence`（LLM 评估的证据质量）：信号可信度
  - 乘积 = 期望信号强度，同时要求方向明确且证据可靠
  - 入选门槛：`composite_score > 0.1`（bullish）且 `confidence >= CONFIDENCE_THRESHOLD`
- **仓位**：波动率目标定总暴露 + 逆波动率定个股权重
  - 总暴露 = `TARGET_VOLATILITY / σ_portfolio`，截断到 `MAX_POSITION_RATIO`
  - 个股权重 `w_i = (1/σ_i) / Σ(1/σ_j)`，σ_i 由 ATR/price 近似
  - 配置：`TARGET_VOLATILITY=0.15`、`FALLBACK_VOLATILITY=0.016`
- **风控**：复用 `risk_manager.py` 硬规则（ATR 动态止损 `ATR_STOP_MULTIPLIER=2.5`、回撤、连亏降仓、换手惩罚 `TURNOVER_PENALTY_RATIO=0.001`）+ 单股上限
- **执行**：回测引擎按调仓周期执行，`recorder.py` 记录

**交付**：`scripts/run_backtest_stage3.py`；完整可回测策略 + 净值曲线 + 绩效指标。

---

### 阶段 4 · 多 Agent + 编排汇总层 + Agent 记忆（2–2.5 周）⏸️

从单 Agent 扩展为多 Analyst 协同，同时引入轻量级 Agent 记忆。

#### 4A. 多 Agent 编排

- **新增 Agent**：fundamental-analyst（读财报关键指标）；可选 macro/风格-analyst
- **编排**（LangGraph）：各 Agent 观点按置信度加权，或 chief Agent 汇总裁决
- **冲突处理**：方向相反时按置信度加权，或降级为中性（记录分歧度）
- **输出**：每只股一个融合后的 `(direction, confidence, 分歧度)`

#### 4B. Agent 记忆（L1 + L-Calibrate）

**L1 · 分析上下文记忆**：让 LLM 每次分析时"记得"自己之前的判断。

- **存储内容**：每次分析的 `(symbol, date, direction, confidence, key_factors)`
- **注入方式**：`_build_user_message` 时拼接最近 N 次该股票的分析历史，含实际结果反馈（通过 `price_series` 计算上次分析后的真实市场收益）
- **注入示例**：
  ```
  上次你分析 AAPL（2026-07-28）的判断：
    direction: bullish, confidence: 0.82
    key_factors: ["iPhone销量超预期", "AI业务增长"]
    → 实际结果：之后5天涨了 +3.2%（判断正确）
  本次新闻如下：[...]
  请分析是否需要调整观点。
  ```
- **存储位置**：复用 `llm_analysis_cache` 表（通过 `get_previous_analysis(symbol, before_date)` 查询上一条记录），无需新建独立表
- **回测开关**：`stock_selector._DISABLE_L1_MEMORY = True`（回测时默认禁用，确保 LLM 缓存命中率；实盘时设为 False 启用闭环记忆）

**L-Calibrate · 置信度校准**：

- **原理**：维护 `(LLM自报confidence区间 → 实际胜率)` 映射表
- **应用**：在 `decide_func` 中用校准后 confidence 替代 LLM 原始输出计算仓位
- **存储**：JSON 文件 `data/cache/llm/calibration.json`（数据量极小、无需 SQL 查询、便于检查调试）
- **分箱**：5 个箱 `[0,0.2),[0.2,0.4)...[0.8,1.0]`，最少 3 个样本才校准
- **论文价值**：独立消融实验——"原始 confidence vs 校准 confidence"的绩效对比

#### 新增模块

- `agents/confidence_calibrator.py`：置信度校准器（JSON 存储，分箱统计）
- `core/data/llm_cache_db.py`：L1 记忆查询（`get_previous_analysis`，复用 `llm_analysis_cache` 表）
- `agents/llm_analyst.py`：记忆注入（`_build_memory_context`，含实际收益反馈闭环）
- 缓存 key 为四元组 `(symbol, date, model, prompt_version)`，prompt 版本由文件内容 hash 自动派生

**交付**：多 Agent 协同信号 + 记忆基础设施；消融实验：无记忆 vs 有记忆（L1）、原始 vs 校准 confidence。

---

### 阶段 5 · PPO 仓位融合器（2 周）⏸️

将阶段 3 的公式仓位替换为 PPO，感知市场风格。

**核心设计：周频选股 × 日频仓位**（方案 B）

- **LLM Analyst**：每周调仓日调用一次，分析所有候选股新闻 → 选出 Top-K（等权持有）→ 缓存信号
- **PPO 融合器**：**日频决策**，输入 = 校准后 LLM 观点（含`signal_age`新鲜度）+ 技术数值 + 市场环境特征 + 账户状态 → 输出总仓位档位（离散：0%/25%/50%/75%/100%）
- **执行逻辑**：
  - **调仓日（周一）**：LLM 刷新选股 → 更新 Top-K；PPO 决定总仓位档位 → 调仓到目标仓位等权分配给 Top-K
  - **非调仓日（周二 - 周五）**：LLM 信号前向填充，`signal_age`逐渐增大；PPO 根据最新技术指标微调总仓位（不换手股票）
- **奖励**：α·log_return − β·drawdown − γ·trade_cost（日频，捕捉周内波动）
- **框架**：Stable-Baselines3 PPO，64×64 网络，纯 CPU，多种子训练

**为何采用方案 B**：
| | 纯周频 (50 样本/年) | 方案 B: 周频选股×日频仓位 (250 样本/年) |
|---|---|---|
| 训练样本量 | ❌ 太少，易过拟合 | ✅ 足够（配合 n_epochs=10 复用~2500 次更新） |
| LLM 调用成本 | ✅ 低（50 次/年） | ✅ 中等（250 次/年，但周频已优化） |
| 应对突发事件 | ❌ 迟钝（只能干挨一周打） | ✅ 灵敏（周三暴跌周四可减仓） |
| 接近真实交易 | ⚠️ 一般 | ✅ 最接近（真实交易员每日看盘定仓位） |
| 状态一致性好坏 | ❌ 差（周中信号过期靠填充） | ✅ 好（每天指标都是新鲜的） |

**状态向量**（紧凑，抗过拟合）：
  - LLM 观点（5 维）+ 技术数值（6 维）+ 市场环境特征（4 维）+ 账户状态（3 维）
  - **新增 `llm_signal_age`**：当前距上次 LLM 分析的天数/7，让 PPO 学会对陈旧观点打折

**动作空间**：离散仓位档位 {0%, 25%, 50%, 75%, 100%}（不变）

**执行约束**：
  - 非调仓日 PPO 只调总仓位，不换手股票（降低无意义频繁交易）
  - 换手惩罚需调大，避免 PPO 学到"每天微调"策略

**交付**：PPO 仓位版策略（日频×周频解耦）；与阶段 3 公式版的对照实验；消融实验——日频 vs 周频 PPO。

---

### 阶段 6 · 科学评估体系（1.5 周）⏸️

- **Walk-forward 滚动验证**：多窗口报告结果分布
- **真实成本**：手续费 + 滑点计入回测与 PPO 奖励
- **多基线对照**：Buy&Hold、随机、规则、公式版、PPO 版
- **统计显著性**：多种子报告均值 ± 方差
- **消融实验**：LLM vs VADER、单 vs 多 Agent、有 vs 无 regime 特征、无记忆 vs L1、原始 vs 校准 confidence

**交付**：完整对照 + 消融 + 滚动验证实验结果与图表。

---

### 阶段 7 · IBKR 模拟盘 Demo（1 周，可选）⏸️

将策略接 IBKR Paper Trading 做答辩演示。训练/实验全部离线完成，IBKR 仅做展示。

---

## 三、评估指标（全程统一）

累计收益率、最大回撤、夏普比率、胜率、盈亏比、换手率、样本外稳定性（多窗口/多种子方差）。

---

## 四、论文实验主线

| 组别 | 内容 | 对应阶段 |
|------|------|----------|
| 对照组 | 规则策略（MA/RSI） | 阶段 0 |
| 实验组 1 | LLM 单 Agent + 公式仓位（无记忆） | 阶段 3 |
| 实验组 2 | LLM 多 Agent + 公式仓位 + L1 记忆 + 置信度校准 | 阶段 4 |
| 实验组 3 | LLM 多 Agent + PPO 仓位融合（主推） | 阶段 5 |
| 消融 · 情绪源 | LLM vs VADER | — |
| 消融 · Agent 数 | 单 vs 多 Agent | — |
| 消融 · 记忆 L1 | 无记忆 vs 分析上下文记忆 | 阶段 4 |
| 消融 · 置信度校准 | 原始 confidence vs 校准 confidence | 阶段 4 |
| 消融 · regime | 有 vs 无 regime 特征 | 阶段 5 |

**核心论点**：LLM 做语义/事件理解，PPO + 量化做数值择时与仓位融合，轻量级 Agent 记忆使策略具备自我校准能力；多源信息 + 风控约束使策略更稳健、回撤更低，而非单纯追求高收益。

---

## 五、Agent 记忆体系

### 设计哲学

当前 LLM Analyst 是**无状态**的——每次调用只看当天新闻，不记得上周判断、不知道历史对错。记忆体系的核心目标是让 Agent 系统**边跑边迭代自身**：从无状态工具 → 有记忆的分析师。

### 已规划：L1 + L-Calibrate（阶段 4 实现）

| 层次 | 存储 | 消费者 | 核心数据 |
|------|------|--------|----------|
| L1 分析上下文 | `llm_analysis_cache` 表（复用） | LLM Analyst（注入 prompt） | 历史分析的 direction/confidence/key_factors + 实际结果（通过 price_series 计算） |
| L-Calibrate 置信度校准 | JSON `calibration.json` | `decide_func`（仓位计算） | LLM 自报 confidence 区间 → 实际胜率映射 |

**反馈闭环**：

```
分析(含历史上下文注入) → 决策(用校准后confidence) → 执行 → 记录结果 → 下次分析(含更新后的历史)
     ↑                                                                                   │
     └──────────────────────────────── 闭环 ────────────────────────────────────────┘
```

**技术选型**：

- **L1 记忆复用 `llm_cache.db`**：不新建独立表，通过 `get_previous_analysis` 查询 `llm_analysis_cache` 中同 symbol 的上条记录
- **校准表用 JSON 文件**：数据量极小（5 个箱），JSON 更轻量、便于检查调试
- **记忆注入有上限**：每次 prompt 最多注入最近 5 条分析历史，避免 token 爆炸

**模块规划**：

| 文件 | 职责 |
|------|------|
| `agents/confidence_calibrator.py` | 置信度校准器（JSON 存储，分箱统计，全局单例） |
| `core/data/llm_cache_db.py` | L1 记忆查询（`get_previous_analysis`），LLM 缓存管理 |
| `agents/llm_analyst.py` | 记忆注入（`_build_memory_context`），四维结构化打分 |

**消融实验**：

| 实验 | 对比 | 预期结论 |
|------|------|----------|
| L1 消融 | 无记忆 vs 有分析上下文 | 减少观点翻转，降低换手率 |
| L-Calibrate 消融 | 原始 vs 校准 confidence | 仓位更准确，夏普提升 |

### 后续优化方向（主框架完善后按需扩展）

| 层次 | 内容 | 价值 | 复杂度 |
|------|------|------|--------|
| L2 策略绩效记忆 | 记录每次调仓快照 + 5 天后收益，给 LLM/PPO 提供绩效反馈 | 让 LLM 学会"高波动时高 confidence 不可靠" | 中 |
| L3 市场模式记忆 | 市场特征向量 + 余弦相似度检索，给 LLM/PPO 提供历史类比 | 让 LLM 学会"当前环境像2024年8月，那次科技股跌了" | 中 |
| L4 Prompt 自进化 | Meta-Agent 定期审视绩效，自动调整分析 Prompt | 系统持续进化，论文亮点 | 高 |
| 决策规则自发现 | 从绩效记忆中自动挖掘高胜率/高亏损模式 | 数据驱动的规则发现 → 规则应用 → 效果验证 | 高 |

---

## 六、待验证的改进假设（阶段 3 后按需实验）

> 以下条目均为**待回测验证的假设**，不是已确认的改进。任何一项都需要通过消融实验（有 vs 无）看夏普比/最大回撤/换手率是否有统计显著提升后再决定是否保留。

### 假设 1：技术面门槛过滤（趋势否决权）

**现状问题**：阶段 3 的选股 100% 由情感面 confidence 排序，技术面不参与。这会导致选出"新闻温和利好但正处于下跌通道"的股票。

**改进思路**：LLM 仍分析全部候选股，但在进入 Top-K 之前加一道客观技术门槛：

```
候选池(10只) → LLM 情感分析 → confidence 排序 → 技术面门槛过滤 → Top-K → 仓位
```

**候选门槛规则**（任选或组合，具体参数待回测确定）：

- 趋势门槛：`ema_short > ema_long`（处于上升趋势才允许入选）
- 动量门槛：近 20 日收益率 > 0
- 波动率调整：`adjusted_conf = confidence / σ_i`，高波动股票需要更强情感才能入选

**实现位置**：`agents/stock_selector.py` 的 `select_top_k` 中，`qualified` 列表之后加过滤。

**为什么可能有效**：动量效应在学术上是稳健异象（Jegadeesh & Titman 1993），"不接下坠的刀"是工程共识。

**为什么可能无效**：EMA 门槛在震荡市会被反复打脸；候选池只有 10 只，门槛过严可能剩不下几只。

**验证方式**：消融实验——"纯情感面 Top-K" vs "情感面 + 趋势门槛 Top-K"，对比夏普比、最大回撤、换手率。

### 假设 2：多因子选股打分

**思路**：把情感面 confidence 和动量/质量因子加权合并为 `final_score`，取代纯 confidence 排序。

```
final_score = w1·normalized_sentiment + w2·normalized_momentum + w3·normalized_quality
```

**待解决问题**：权重 w1/w2/w3 如何确定（人工拍 vs 历史回归 vs 风险平价）；各因子量纲如何归一化。

**验证方式**：网格搜索权重 + walk-forward 验证，避免过拟合。

### 假设 3：新闻覆盖率偏差修正

**现状问题**：LLM prompt 中 confidence 曾与新闻条数存在倾向性关联（5+ 条 → 0.8-1.0），导致大票天然占优。

**已缓解**：prompt 已更新置信度评估标准，明确区分「多条独立事件印证→高」与「同一事件多源报道→中」，并引入信息增量递减原则，解耦了 confidence 与新闻条数的硬绑定。

**候选改进**（若缓解不足）：

- 在选股时对 confidence 做覆盖率归一化（`confidence / log(1 + news_count)`）

**验证方式**：统计不同股票的 news_count 分布 + 消融实验。

### 假设 4：新闻分段缓存锚定到固定日历网格 ✅ 已实现

**现状问题**：`_fetch_news_segmented_av` 的段边界从请求的 `start_date` 开始按 `NEWS_SEGMENT_DAYS` 步进，段窗口随请求起点漂移。不同时间/不同区间拉取会产生**重叠的分段缓存**（缓存目录已出现 30 天与 14 天两套重叠段，如 `AAPL_news_seg_2025-08-07_2025-09-05` 与 `..._2025-08-11_2025-08-24`），同一批新闻被重复请求，浪费本就稀缺的 Alpha Vantage 配额（25 次/天，PPO 训练数据瓶颈）。

**改进思路**：段边界锚定到**全局固定网格**——选一个全局锚点（周一），段 = `[anchor + Nk, anchor + Nk + (N-1)]`。任何请求区间都映射到同一批段文件，天然去重、跨回测复用。选 28 天而非 14 天：AV 单次上限 1000 条，28 天段（约 829 条）更接近限制但不超限，减少段数和 API 调用次数。

**实现位置**：`config.py`（`NEWS_SEGMENT_ANCHOR="2020-01-06"`、`NEWS_SEGMENT_DAYS=28`）、`core/data/news_data.py`（`_fetch_news_segmented_av` 段循环改为网格对齐）；配套一次性归并脚本把旧重叠段重整到新网格并归档。

**为什么可能有效**：消除重复请求 → 相同配额覆盖更长历史。**为什么可能无效/风险**：网格末段会拉取略超回测 end_date 的数据（PIT 消费端已过滤，无未来函数风险）。

**验证方式**：对比重整前后同一区间的实际 AV 请求次数与缓存命中率。

### 假设 5：调仓日锚定到固定日历（每周第一个交易日） ✅ 已实现

**现状问题**：`MultiStockBacktestEngine` 的 `rebalance_counter` 从 `all_dates[0]` 计数，调仓日随回测起点漂移（7.6 起始 vs 7.7 起始 → 整串调仓日错位）。LLM 缓存按 `(symbol, analysis_date, model)` 精确日期为 key，调仓日漂移导致同一周被重复分析、**跨回测无法复用 LLM 缓存，浪费 GLM-4 token**。

**改进思路**：调仓日 = 每个自然周（ISO 周）的第一个可交易日，不再依赖起点计数。任何起点下，从第二个自然周起调仓日完全对齐 → LLM 缓存 key 稳定、跨回测复用；并与 7 天新闻 lookback 窗口对齐。用 `config.REBALANCE_WEEKLY` 开关保留旧 `REBALANCE_DAYS` 计数模式以便消融。

**实现位置**：`config.py`（`REBALANCE_WEEKLY`）、`core/multi_stock_engine.py`（调仓判定改为 ISO 周切换）。

**验证方式**：不同起点跑同一后续区间，统计 LLM 缓存命中率提升；确认净值曲线在对齐区间一致。

### 假设 6：PPO 状态向量新增「信号新鲜度」维度 ✅ 已实现

**现状问题**：LLM 信号为周频（仅在调仓日更新），PPO 训练环境按日频步进，非调仓日使用上一次信号的前向填充，PPO 无法感知手上观点已过期几天，可能导致对陈旧信号的过度信任或错误依赖。

**改进思路**：状态向量新增一维 `llm_signal_age`（当前日距最近一次 LLM 信号的天数，归一化为 0-4），让 PPO 学会根据信号新鲜度自适应仓位策略。

- **PPOEnv（训练）**：实时计算真实 age = `(current_date - last_llm_signal_date).days / 7`，clip 到 [0, 4]
  - 调仓日：age = 0（信号新鲜）
  - 周二：age ≈ 0.14（过期 1 天）
  - 周五：age ≈ 0.57（过期 4 天）
- **PPOPolicy（回测推理）**：仅调仓日决策 → signal_age = 0（始终新鲜）
- **效果**：PPO 通过历史经验学会"高波动市里，信号已经过期 4 天了，应该轻仓避险；趋势行情里，过期也没关系，可以继续重仓"

**预期价值**：在剧烈波动的市场环境中，能主动降低仓位以应对信息滞后风险。

**实现位置**：`core/ppo/ppo_env.py`（`STATE_DIMS` + `_compute_llm_features`）、`core/ppo/ppo_policy.py`（`_build_state_vector` 补齐同维）。

**验证方式**：消融实验——有 vs 无 `signal_age` 维度，重点观察周中减仓响应能力和夏普比率/最大回撤变化。

### 假设 7：LLM 输出改为「双正交连续信号」并接入 PPO ✅ 已实现（A 档 + B 档）

**核心设计**：LLM 产出两个正交的连续量，分别承担不同职责：
- `composite_score` ∈ [-1, 1]：**信号方向+强度**，代码从 per_news 四维打分确定性计算（`tanh(Σ(impact+gap)×timeliness×certainty / SCALE)`）
- `confidence` ∈ [0, 1]：**证据质量/可信度**，LLM 基于 prompt 中明确定义的标准评估（多条独立事件印证→高，数量不足/矛盾/同一事件多源报道→中，稀少或噪音→低）；prompt 引导 LLM 在 per_news 打分时对同源重复报道递减计分（信息增量递减）
- **选股排序**：`ranking_score = composite_score × confidence`（期望信号强度，无参数、无阈值、数学上有期望值解释）
- `direction` = `sign(composite_score)`（±0.1 死区），仅用于日志和人类可读性，不参与决策逻辑

**聚合公式改为「求和 + 压缩」**（终结权重之争）：
```
raw = Σ_i (impact_i + gap_i) · timeliness_i · certainty_i     # 最近≤10条新闻
composite_score = tanh(raw / SCALE)                              # SCALE≈6，唯一待标定旋钮
```
- 中性噪音 score≈0 加进求和不改变结果 → 天然不稀释（解决"等权平均被噪音稀释"问题）
- 多条同向新闻自动叠加（corroboration 增强）；单条极端事件自动主导
- 无需任何权重方案；certainty 仅作乘数用一次，消除双重计权
- `direction = sign(composite_score)`（保留 ±0.1 死区），仅用于日志和人类可读性

**分层落地**：
- **A 档（本假设先行，无副作用）** ✅：仅改 `_compute_scores` 聚合为求和 + tanh，定 SCALE（`_COMPOSITE_SCALE=6.0`）；选股逻辑不变。
- **B 档（含 PPO 收益，需重训模型）** ✅：`llm_cache_db.py` 加 `composite_score` 列 + 迁移 ✅；`run_backtest_stage5.py` 查询补该列写入 `llm_signals` ✅；`ppo_env.py` / `ppo_policy.py` 把 `bullish_ratio`→`mean_signed_score`、`sentiment_std`→`max_abs_signed_score`（保持 18 维不变，仅升级语义） ✅；选股排序改为 `composite_score × confidence` ✅。**改后 `models/ppo_medium` 失效，须重训。**

**为什么可能有效**：连续有符号信号信息量远大于三档标签/方向计数，且已归一化，利于 RL 收敛；结构化分析不再空转。**为什么可能无效/风险**：SCALE 与 tanh 饱和点需标定；PPO 特征语义变更需重训与重新调参，短期指标可能波动。

**验证方式**：A 档——对比改前后同区间 `signed_score` 分布与选股结果是否更合理；B 档——消融实验"bullish_ratio vs mean_signed_score"作为 PPO LLM 特征，对比夏普比/最大回撤/换手率。

---

## 七、MVP 路径

时间紧张时，**阶段 0 → 1 → 2 → 3 即构成完整可答辩的毕设**。阶段 4/5/6 为增强拔高（记忆体系是阶段 4+ 的核心亮点），阶段 7 为演示。卡壳时优先保 LLM 分析层与科学评估，PPO 可降级为公式版，记忆层可只做 L1 + L-Calibrate（工作量最小但论文价值高）。
