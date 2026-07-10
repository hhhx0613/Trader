# 量化交易Agent毕设最终落地方案（单Agent→多Agent｜IB模拟兼容）

## 一、项目总体定

本项目设计并实现一套**从简单到复杂、可迭代升级的量化智能交易Agent系统**。

开发路线：**规则式单Agent最小闭环 → 轻量化PPO强化学习单Agent → 模块化解耦 → 多Agent协同架构 → IBKR模拟盘实盘验证**。

适配硬件条件：**GPU算力低、少量行情数据即可完成训练与回测**。

数据方案：**yfinance / Alpha Vantage / IBKR 三层数据源兜底 \+ 本地缓存**，保证项目稳定性。

## 二、整体技术栈（最终确定）

- **数据层**：yfinance（主力）、Alpha Vantage（兜底）、IBKR API（最终兜底\+实盘模拟）、本地CSV缓存 + SQLAlchemy ORM（默认SQLite，后续可无缝切换PostgreSQL）

- **框架层**：Python、PyTorch（轻量模型）、Pandas、NumPy、SQLAlchemy（数据库ORM）

- **算法层**：传统技术指标策略（MA/RSI/MACD）\+ 轻量化PPO强化学习（Stable-Baselines3）

- **交易层**：自研本地回测撮合引擎 \+ IBKR模拟盘Paper Trading双后端

- **评估层**：收益率、最大回撤、夏普比率、胜率、盈亏比

## 三、核心设计原则（保证你毕设好做、稳过）

1. **先跑通，再优化**：第一阶段只做最简可运行闭环，不堆复杂算法

2. **先离线、后在线**：全部训练\&实验在本地回测完成，IB仅做最终演示

3. **先单Agent、再多Agent**：功能稳定后再拆分子智能体

4. **无GPU优先策略**：小网络、少特征、少迭代，保证CPU可训练

5. **数据兜底机制**：三层数据源\+本地缓存，彻底解决缺数据、接口报错问题

## 四、五阶段最终开发流程（逐阶段落地，可直接作为论文章节）

### 阶段1：最简规则式单Agent（0训练、0算力压力）

**目标**：搭建整套系统底层框架，跑通完整交易闭环

**核心功能**：

- 实现三层数据源采集（优先yfinance）\+ 本地缓存

- K线清洗、标准化Bar数据结构（统一开高低收、时间、成交量）

- 基础技术指标计算（MA、RSI、MACD）

- 规则式交易决策（金叉买、死叉卖、超买超卖）

- 自研本地回测撮合引擎（手续费、滑点、仓位管理）

- 极简风控：最大仓位、单笔止损、单日最大回撤

- 交易记录保存、净值曲线、回测指标评估

**阶段成果**：可运行Demo、完整回测报告、稳定底层架构

**优势**：无需训练、无需GPU、数据量需求极小

### 阶段2：升级为轻量化PPO单Agent（CPU可训练）

**目标**：把固定规则替换成AI自主学习决策，完成论文创新点

**核心改动与安全约束设计（杜绝AI发疯/失控）**：

- 保留阶段1所有底层框架、数据、撮合、风控、评估代码，**保留传统规则作为兜底护栏**

- 移除纯硬编码全程决策，接入**极简PPO网络（2层小网络）**，由AI负责择优择时

- 动作空间简化：空仓、半仓、满仓（三分类，极低算力、稳定性强）

- 输入特征仅用基础技术指标（10维以内），避免特征杂乱导致模型漂移

- 本地历史行情反复交互训练，依托PPO原生clip裁剪机制限制策略更新幅度，防止训练突变崩坏

- 模型保存、训练日志、回测对比，增加KL散度、奖励稳定性监控

- **关键安全架构（核心防失控\+策略分工）**：采用「AI自主决策 \+ 规则硬护栏」双轨分工架构。PPO完全替代传统人工交易策略（海龟、均线、RSI固定买卖逻辑），自主学习择时与仓位优化；同时保留独立风控硬规则作为终审屏障，高波动、连续回撤、无趋势行情下强制覆盖AI激进决策，杜绝AI乱开仓、重仓、逆势交易的失控问题，兼顾策略智能性与交易安全性

**阶段成果**：AI智能交易Agent，可自主学习择时

**适配你的硬件**：纯CPU训练，单轮训练1–3分钟跑完

### 阶段3：代码模块化解耦（多Agent架构前置铺垫）

**目标**：把单体代码拆成标准模块，为后续多Agent铺路

**拆分模块**：

- DataCollector 数据采集模块（含三层兜底\+缓存）

- StrategyBrain 决策模块（规则/PPO可切换）

- RiskController 风控模块

- TradeExecutor 交易执行模块（本地/IB双后端）

- Recorder 复盘评估模块

**阶段成果**：高解耦、可扩展、可插拔的工程架构

### 阶段4：迭代升级为多Agent架构（论文拔高亮点）

**目标**：从单Agent进化为多智能体协同系统，解决纯PPO单维度信息易过拟合、泛化性弱的问题，通过分工协作提升策略稳定性（核心毕设创新点）

**架构范式**：采用 **CTDE（Centralized Training with Decentralized Execution，集中训练-分布执行）** 架构，这是多智能体强化学习（MARL）领域的经典范式。各子Agent独立运行、独立产出信号，PPO主控Agent集中接收所有信号进行训练和决策。

**通信协议**：**同步流水线（Synchronous Pipeline）**。每个时间步（Bar）按固定顺序执行：数据采集 → 各子Agent并行计算特征 → 特征拼接 → PPO决策 → 风控审核 → 下单执行。选择同步而非异步的原因：①回测场景下时间步天然有序，异步无收益；②实现简单、调试方便、结果可复现；③CPU场景下并行计算收益有限，同步流水线更稳定。

---

#### 4.1 技术因子Agent（TechnicalFactorAgent）

**职责**：从原始行情数据中挖掘、筛选、降噪高质量量化因子，为PPO提供信息密度更高的多维特征输入，替代阶段2中人工挑选的简单指标。

**输入**：
- 原始OHLCV K线数据（开高低收量），窗口长度 = 60根Bar（约3个月日线）

**数据来源**：DataCollector模块（yfinance / Alpha Vantage / 本地缓存）

**处理流程与算法**：
1. **因子计算层**：基于OHLCV计算 25+ 维候选因子池
   - 趋势类：SMA(5/10/20/60)、EMA(12/26)、ADX(14)、Aroon(25)
   - 动量类：RSI(14)、MACD(12,26,9)、Stochastic(14,3,3)、Williams %R(14)、CCI(20)
   - 波动类：Bollinger Bands(20,2)、ATR(14)、NATR(14)、历史波动率(20)
   - 量价类：OBV、VWAP、量比、MFI(14)
   - 所有指标使用 `ta` 库（Python Technical Analysis Library）统一计算
2. **因子筛选层**：
   - **缺失值过滤**：剔除前N根Bar无法计算的因子（如SMA60在前59根Bar无值），用NaN填充后丢弃
   - **相关性过滤**：计算因子间Pearson相关矩阵，相关系数 > 0.85 的因子对只保留与未来收益率相关性更高的一个（去除冗余）
   - **重要性排序**：使用 `sklearn.ensemble.RandomForestRegressor` 对因子做特征重要性排序（CPU秒级完成），选取Top-15因子
3. **归一化层**：对筛选后的15维因子做 **Z-Score标准化**（滚动窗口均值/标准差），保证输入PPO的特征在同一量级

**输出**：15维归一化技术因子向量 `tech_features: np.ndarray, shape=(15,)`

**代码结构**：
```python
class TechnicalFactorAgent:
    def __init__(self, n_factors=15, lookback=60): ...
    def compute_raw_factors(self, ohlcv: pd.DataFrame) -> pd.DataFrame: ...
    def select_factors(self, factors: pd.DataFrame, returns: pd.Series) -> list: ...
    def normalize(self, factors: pd.DataFrame) -> np.ndarray: ...
    def get_features(self, ohlcv: pd.DataFrame) -> np.ndarray:  # 主入口，返回(15,)
```

---

#### 4.2 舆情Agent（SentimentAgent）

**职责**：从外部新闻/舆情数据中提取市场情绪信号，弥补纯量价模型无法感知事件驱动（如财报、政策、黑天鹅）的短板。

**输入**：
- 股票代码 + 当前日期
- 新闻文本数据（标题 + 摘要）

**数据来源**（按优先级）：
1. **Finnhub News API**（主力，免费）：`https://finnhub.io/api/v1/news?symbol=AAPL&token=xxx`，每日限额60次，返回新闻标题+摘要
2. **Alpha Vantage News Sentiment API**（兜底，免费）：返回新闻 + 内置情绪评分（-1到1），直接可用
3. **本地缓存情绪文件**（最终兜底）：预计算好的每日情绪分数CSV，保证离线训练不依赖网络

**处理流程与算法**：
1. **文本情绪打分**：使用 **VADER（Valence Aware Dictionary for sEntiment Reasoning）** 规则式NLP模型
   - 无需GPU、无需下载大模型，`pip install vaderSentiment` 即可
   - 对每条新闻标题+摘要计算 compound score（-1 到 +1）
   - VADER 是金融文本情绪分析的经典基线方法，论文引用量高
2. **情绪聚合**：当日所有新闻的 compound score 取均值，得到 `daily_sentiment`
3. **情绪特征工程**：构造 5维 情绪特征向量：
   - `sentiment_today`：当日情绪均值
   - `sentiment_3d_ma`：3日情绪移动平均（平滑噪声）
   - `sentiment_7d_ma`：7日情绪移动平均（趋势）
   - `sentiment_std_3d`：3日情绪标准差（分歧度）
   - `sentiment_change`：当日 vs 昨日情绪变化率（情绪动量）

**输出**：5维情绪特征向量 `sentiment_features: np.ndarray, shape=(5,)`

**代码结构**：
```python
class SentimentAgent:
    def __init__(self, cache_dir="./cache/sentiment"): ...
    def fetch_news(self, symbol: str, date: str) -> list: ...  # Finnhub → AlphaVantage → 缓存
    def score_text(self, text: str) -> float: ...  # VADER打分
    def aggregate_daily(self, scores: list) -> float: ...
    def build_features(self, symbol: str, date: str) -> np.ndarray:  # 主入口，返回(5,)
```

**离线训练兼容方案**：PPO训练时需遍历历史数据，不可能逐日调API。解决方案：
- **预下载**：在训练前一次性拉取全部历史新闻情绪，存为本地CSV
- **缺失兜底**：历史日期无新闻数据时，情绪特征填0（中性），不影响训练

---

#### 4.3 风控Agent（RiskControlAgent）

**职责**：独立于PPO决策之外的安全护栏，实时监控组合风险状态，输出风险约束信号。在PPO决策过于激进时强制覆盖，保障交易安全边界。

**输入**：
- 当前账户状态：净值、持仓、浮盈亏、历史交易记录
- 当前市场行情：近20日收益率序列（用于计算波动率）

**数据来源**：Recorder模块（账户状态）+ DataCollector模块（行情数据）

**风控规则（纯规则，无需训练）**：

| 规则编号 | 触发条件 | 动作 |
|---------|---------|------|
| R1 | 单笔亏损 > 总资金2% | 强制平仓，本次信号忽略 |
| R2 | 单日回撤 > 总资金5% | 当日禁止新开仓 |
| R3 | 连续亏损 ≥ 3笔 | 仓位上限降为半仓，持续5个Bar |
| R4 | 20日波动率 > 历史90分位 | 仓位上限降为半仓 |
| R5 | 总回撤 > 15% | 全部清仓，暂停交易10个Bar |
| R6 | PPO输出仓位 > 风控允许上限 | 截断至允许上限 |

**输出**：3维风控信号向量 `risk_signals: np.ndarray, shape=(3,)`
- `risk_signals[0]`：仓位上限系数（0.0 ~ 1.0，0=禁止交易，1=无限制）
- `risk_signals[1]`：风险等级（0=正常，1=警戒，2=危险）
- `risk_signals[2]`：是否强制平仓标志（0=否，1=是）

**代码结构**：
```python
class RiskControlAgent:
    def __init__(self, max_drawdown=0.15, single_loss_limit=0.02): ...
    def check_rules(self, portfolio, market_data) -> dict: ...  # 逐条检查
    def get_risk_signals(self, portfolio, market_data) -> np.ndarray:  # 主入口，返回(3,)
    def override_action(self, action: int, risk_signals: np.ndarray) -> int:  # 覆盖PPO决策
```

---

#### 4.4 主控Agent（MasterAgent / PPO决策器）

**职责**：汇总所有子Agent的输出特征，拼接为完整状态向量，输入PPO网络做出最终交易决策。

**状态向量拼接**：
```
state = concat([
    tech_features,       # (15,) 来自技术因子Agent
    sentiment_features,  # (5,)  来自舆情Agent
    risk_signals,        # (3,)  来自风控Agent
    portfolio_features   # (4,)  当前持仓比例、浮盈亏率、连续亏损次数、账户净值变化率
])
# 总维度 = 15 + 5 + 3 + 4 = 27维
```

**PPO网络结构**（继续使用Stable-Baselines3）：
```
Actor网络:  Linear(27, 64) → ReLU → Linear(64, 64) → ReLU → Linear(64, 5)
Critic网络: Linear(27, 64) → ReLU → Linear(64, 64) → ReLU → Linear(64, 1)
```
- 动作空间：**离散5档仓位** = {0%, 25%, 50%, 75%, 100%}，比阶段2的3档更精细
- 训练框架：`stable_baselines3.PPO`，参数：`n_steps=2048, batch_size=64, n_epochs=4, gamma=0.99, clip_range=0.2`

**奖励函数设计**：
```
reward = alpha * log_return - beta * drawdown_penalty - gamma * trade_cost
```
- `log_return`：对数收益率（鼓励盈利）
- `drawdown_penalty`：当前回撤深度（惩罚风险）
- `trade_cost`：换手惩罚（抑制过度交易）
- 建议参数：`alpha=1.0, beta=0.5, gamma=0.01`

---

#### 4.5 训练方式：集中式训练

**训练架构**：**集中式（Centralized）**，非分布式。

**具体流程**：
1. 各子Agent的因子计算/情绪计算/风控计算逻辑**在训练前预计算完毕**，缓存为numpy数组
2. 训练时，PPO环境（Gym接口）在每个step中：
   - 从预计算缓存中取出当前时间步的 `tech_features`, `sentiment_features`, `risk_signals`, `portfolio_features`
   - 拼接为27维state，返回给PPO
   - PPO输出action（0-4对应5档仓位）
   - 风控Agent的 `override_action()` 做最终审核
   - 执行交易、计算reward
3. 使用Stable-Baselines3标准PPO训练循环，**无需自定义分布式训练代码**

**为什么不用分布式训练**：
- 本项目的"多Agent"是**功能分工**（不同Agent负责不同维度的信息处理），而非**对抗/博弈**场景
- 各子Agent没有独立的学习目标，它们的目标统一为"辅助PPO做出更好的交易决策"
- 分布式训练（如MADDPG、MAPPO）适用于多Agent各自有独立奖励函数的场景（如多人博弈），本项目不适用
- **论文中的表述**：采用CTDE架构，各Agent分布式执行（独立计算特征），PPO集中式训练（统一优化决策策略）

---

#### 4.6 完整执行时序（单个时间步）

```
时间步 t：
  ┌─────────────────────────────────────────────────────┐
  │ 1. DataCollector 获取 Bar_t (OHLCV)                  │
  │ 2. TechnicalFactorAgent.get_features(ohlcv) → (15,) │
  │ 3. SentimentAgent.build_features(symbol, date) → (5,)│
  │ 4. RiskControlAgent.get_risk_signals(portfolio) → (3,)│
  │ 5. 拼接 state = (27,)                                │
  │ 6. PPO.predict(state) → action (0-4)                 │
  │ 7. RiskControlAgent.override_action(action) → final   │
  │ 8. TradeExecutor.execute(final_action)                │
  │ 9. Recorder.log(trade, portfolio, reward)             │
  └─────────────────────────────────────────────────────┘
```

步骤2、3、4之间**无依赖关系**，理论上可并行（`concurrent.futures.ThreadPoolExecutor`），但在回测场景下串行执行即可（CPU开销极小）。

---

#### 4.7 与阶段2单Agent的对比（论文实验核心论点）

| 维度 | 阶段2 单Agent PPO | 阶段4 多Agent PPO |
|------|------------------|------------------|
| 输入特征 | 人工挑选 ~10维基础指标 | 自动筛选 15维因子 + 5维情绪 + 3维风控 = 27维 |
| 信息来源 | 仅量价 | 量价 + 舆情 + 风险状态 |
| 风控方式 | 简单硬规则 | 独立风控Agent，6条规则，输出连续风控信号 |
| 动作空间 | 3档仓位 | 5档仓位 |
| 特征工程 | 人工 | 因子Agent自动筛选+归一化 |
| 预期优势 | 基线 | 更低回撤、更高夏普（多源信息+风控约束） |

**阶段成果**：完整多智能体量化交易系统（毕设最高创新点）
**预计工期**：2-3周（因子Agent 1周 + 舆情Agent 0.5周 + 主控改造+训练 1周）

### 阶段5：接入IBKR模拟盘Paper Trading（最终演示亮点）

**目标**：离线回测有效策略，落地到真实模拟交易环境

**功能**：

- 切换执行后端：本地回测 → IBKR实时模拟交易

- 实时行情订阅、实时仓位查询、自动调仓

- 实盘约束验证：滑点、价差、交易时段、保证金

**关键原则**：**训练、实验、对比全部在本地完成，IB只做展示**

**实时指标计算优化（增量计算）**：

阶段1的回测场景下，`compute_all_indicators()` 对全量历史数据重算（1600行 ≈ 5ms），性能完全足够。但进入阶段5实时行情后，每秒可能多次收到新 Bar，需优化为**增量计算**模式：

- **EMA/MA**：保留上一时刻的 EMA 值，新 Bar 到达时仅用递推公式更新一行：`ema_new = price * alpha + ema_prev * (1-alpha)`
- **RSI**：同理，保留 `avg_gain/avg_loss`，只算最新一行
- **MACD**：内部就是 EMA 递推，天然支持增量
- **ATR**：保留上一时刻 ATR，用 Wilder 平滑更新
- **VWAP**：保留累积 `sum(tp*vol)` 和 `sum(vol)`，新 Bar 累加即可

实现方式：在 `indicators.py` 中新增 `update_indicators(last_state, new_bar)` 函数，接收上一时刻的指标状态 + 新 Bar，返回更新后的状态 + 指标值，避免全量重算。回测时仍用全量计算（保证正确性），实时模式切换到增量计算（保证响应速度）。

## 五、数据源最终兜底策略（固定不变）

**优先级：本地缓存 → yfinance → Alpha Vantage → IBKR**

- 优先读取本地缓存，避免重复下载

- yfinance：主力离线回测、PPO训练数据源

- Alpha Vantage：yfinance失效时兜底补数据

- IBKR：最终兜底 \+ 实时模拟行情源

## 六、论文实验方案（直接写进论文）

**对照组**：传统规则量化策略（MA/RSI）

**实验组1**：单Agent轻量化PPO强化学习策略

**实验组2**：多Agent协同PPO策略（因子/舆情多维特征输入\+风控约束\+AI自主决策）

**对比指标**：累计收益、最大回撤、夏普比率、胜率、稳定性

**结论逻辑**：多Agent融合风控与多因子信号，比单策略更稳健、回撤更低

## 七、本方案最大优势（完全解决你的顾虑）

- ✅ 不需要GPU，普通笔记本全程可跑

- ✅ 不需要大量数据集，单标的几年日线足够

- ✅ 不依赖大模型，核心闭环稳定不翻车

- ✅ 先简单后复杂，每一步都有成果，不会烂尾

- ✅ 单Agent→多Agent演进逻辑清晰，论文非常好写

- ✅ 自带IB模拟盘演示，答辩视觉效果饱满

## 八、最终项目架构一句话总结

本项目以**多层兜底行情数据 \+ 本地离线回测引擎**为底座，从**规则式单Agent**起步，迭代升级为**轻量化PPO智能决策单Agent**，进一步模块化拆解为**多智能体协同交易架构**，最终对接**IBKR模拟盘**完成全流程落地验证，实现了一套低算力、高稳定、可迭代的AI量化交易系统。

> （注：部分内容可能由 AI 生成）
