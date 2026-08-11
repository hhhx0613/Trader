# 量化交易 LLM 多 Agent 系统毕设

> **一句话总结**：LLM 分析新闻做选股，PPO 训练总仓位控制，规则基线作为对照。  
> **当前状态**：✅ 阶段 0-4 全部完成（无 PPO，纯规则+LLM 决策验证）

---

## 📋 核心架构

```
┌─────────────────────────────────────┐
│   Stage 5-7 (未来) ⏸️                │
│   - PPO 强化学习                    │
│   - 多 Agent 协同                   │
│   - IBKR 模拟盘                      │
└─────────────────────────────────────┘
                ↓
┌─────────────────────────────────────┐
│   Stage 1-4 ✅ COMPLETED              │
│   ┌───┐  ┌──────┐  ┌──────────┐     │
│   │数  │→│情绪/决策 │ →│回测撮合  │     │
│   │据  │  │(LLM/VADER)│ │           │
│   └───┘  └──────┘  └──────────┘     │
│  规则基线 / LLM Top-K / VADER Top-K    │
│   ↑                                 │
│  Stage 4: L1 记忆 + 消融实验         │
└─────────────────────────────────────┘
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Keys

复制 `.env.example` 为 `.env`，填入 API Keys：

| API | 用途 | 注册地址 |
|-----|------|----------|
| `FINNHUB_API_KEY` | 新闻数据（兜底，60 次/天） | https://finnhub.io/register |
| `ALPHA_VANTAGE_API_KEY` | 新闻数据（主力，25 次/天） | https://www.alphavantage.co/support/#api-key |
| `GLM_API_KEY` | **Llm 分析新闻**（必填！） | [智谱 AI 控制台](https://open.bigmodel.cn/dev/api/key) |

---

## 🔧 运行回测

一条命令跑通阶段 1-3（三条路径对比）：

```bash
python scripts/run_backtest_stage3.py
```

**默认参数**：
- 候选池：AAPL, NVDA, MSFT, GOOGL, AMZN, JPM, V, JNJ, UNH, WMT
- 时间范围：最近一个月（config.py 中 DEFAULT_START_DATE / DEFAULT_END_DATE）
- Top-K：5 只，每 5 天调仓

**输出对比表**（最近一个月 2026-07-06 ~ 2026-08-06）：

| 路径 | 累计收益率 | 最大回撤 | 夏普比率 | 交易次数 |
|------|-----------|---------|---------|---------|
| 规则基线（均值） | -1.14% | 3.12% | - | 7 |
| LLM Top-K | **+1.85%** | 1.29% | **2.99** | 16 |
| VADER Top-K | +1.20% | 0.97% | 2.24 | 16 |
| Buy&Hold(AAPL) | -0.53% | 10.78% | - | - |

> **说明**：LLM Top-K 表现最好（+1.85%），夏普 2.99，回撤仅 1.29%。

---

## 📊 Stage 4: L1 记忆消融实验

2026-08-11 完成了 L1 记忆的效果验证：

### 实验设计

对比两组配置：
1. **无 L1 记忆**：每天独立判断，不记得昨天的结论
2. **有 L1 记忆**：能看到上次的判断结果，要求解释变化原因

### 实验结果

| 实验 | 收益率 | 最大回撤 | Sharpe |
|------|--------|----------|--------|
| 无 L1 记忆 (baseline) | +0.95% | -0.62% | 2.34 |
| 有 L1 记忆 | **+1.64%** | -0.66% | **4.65** |

**关键发现**：
- ✅ 收益提升 **73%** (0.95% → 1.64%)
- ✅ Sharpe **翻倍** (2.34 → 4.65)
- ✅ 交易次数增加 (23 → 28)，说明更积极参与机会

**机制解释**：L1 记忆增加了判断的连贯性约束，避免"反复横跳"式的噪声决策。

**消融实验已保存到**：`output/ablation/ablation_results.csv`

---

## 📂 项目结构

```
Trader/
├── agents/                    # 决策模块
│   ├── llm_analyst.py        #   GLM-4 LLM 分析新闻
│   ├── stock_selector.py     #   Top-K 选股（持有优先）
│   └── decision_func.py      #   决策函数（LLM /VADER 双路径）
├── core/                      # 核心引擎
│   ├── data/                  #   数据采集
│   │   ├── market_data.py    #     行情（yfinance 优先）
│   │   ├── news_data.py      #     新闻（AV 优先 +Finnhub 兜底）
│   │   ├── sentiment.py      #     VADER 情绪打分
│   │   └── llm_cache_db.py   #     LLM 缓存（SQLite）
│   ├── backtest_engine.py    #   单股回测
│   ├── multi_stock_engine.py #   多股回测（Top-K 组合）
│   ├── risk_manager.py       #   风控（止损/回撤/连亏降仓）
│   ├── strategy.py           #   规则策略（MA/RSI/MACD 投票）
│   └── indicators.py         #   技术指标计算
├── data/cache/                # 本地缓存（不上传 git）
│   ├── market/               #   行情 CSV
│   ├── news/                 #   新闻 CSV（合并 + 分段）
│   └── llm/                  #   LLM 分析结果 SQLite
├── scripts/
│   ├── run_backtest_stage3.py # 端到端回测入口
│   ├── ablation_experiment.py # Stage 4: L1 记忆消融实验
│   ├── clear_llm_cache.py     # 清除 LLM 缓存工具
│   └── data_fetch_tracker.py  # 新闻数据拉取进度追踪
├── docs/                      # 文档
│   └── Plan.md               #   详细设计文档
├── config.py                  # 全局配置
└── main.py                    # 单股回测入口
```

---

## 🔑 关键机制

### 数据层
- ✅ **Point-in-Time**：新闻严格按发布时间对齐，防前视偏差
- ✅ **三层兜底**：本地缓存 → Alpha Vantage → Finnhub
- ✅ **智能缓存**：基于全局锚点网格的段级缓存，天然去重、跨回测复用
- ✅ **分段拉取**：自动将长历史切成 14 天一段逐段请求，最大化 API 配额利用率

### 决策层
- ✅ **LLM 缓存**：SQLite 存储，`(symbol, date, model)` 唯一键，避免重复调用
- ✅ **Top-K 等权 + 持有优先**：选出的股票等权分配，已在持仓的不卖
- ✅ **RSI 自适应**：超买时置信度下降 → 仓位缩减

### 回测层
- ✅ **防前视偏差**：T-1 收盘生成信号 → T 开盘撮合执行
- ✅ **风控硬规则**：止损 2% / 回撤降仓 / R4 连亏降仓
- ✅ **波动率目标仓位**：exposure = target_vol / portfolio_vol × confidence

---

## 📊 已知问题与待办

- [ ] evaluate() 缺少换手率指标
- [ ] PPO 环境（gym.Env）尚未实现
- [ ] IBKR 模拟盘接入（阶段 5）

---

## 📝 Version History

- **v1.2 (2026-08-11)**: Stage 4 - L1 记忆模块完成
  - ✅ L1 记忆：LLM 分析时注入上次判断结果（从 SQLite 缓存加载）
  - ✅ 消融实验：无记忆 vs 有记忆，证明有效性 (+73% 收益提升)
  - ✅ SQLite `get_previous_analysis()` 方法支持历史检索
  - ✅ 新增：`confidence_calibrator.py` (置信度校准器骨架)
  - ✅ 新增：`ablation_experiment.py` (消融实验脚本)
  - ✅ 新增：`clear_llm_cache.py`, `data_fetch_tracker.py` 工具脚本

- **v1.1 (2026-08-11)**: 项目结构优化
  - 缓存分目录：market/ news/ llm/
  - LLM 缓存迁移到 SQLite（`data/cache/llm/llm_cache.db`）
  - 智能缓存匹配：自动复用更大范围的缓存文件
  - 清理一次性脚本，只保留核心文件

- **v1.0 (2026-08-06)**: 阶段 0-3 全部完成
  - 重构：抽取 `BaseBacktestEngine` 消除重复代码
  - 修复：`rebalance_counter` 卡死 bug、空 DataFrame 崩溃
  - 新增：端到端验证脚本、LLM/VADER 双路径
  - 优化：新闻源优先级从合并改为"AV 优先+Finnhub 兜底"
