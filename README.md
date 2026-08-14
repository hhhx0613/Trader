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

### 0. 配置 Python 环境（Conda）

**推荐方式：使用 Conda 的 `trader` 环境**

```bash
# 1. 创建 conda 环境（如果尚未创建）
conda create -n trader python=3.10 -y

# 2. 激活环境
conda activate trader

# 3. 安装依赖
pip install -r requirements.txt
```

**注意**: 本项目要求 Python >= 3.10，建议使用 Miniconda 或 Anaconda。

---

### 1. 安装依赖

#### 如果你已经激活了 Conda 环境：

```bash
pip install -r requirements.txt
```

#### 或者直接使用 Conda 安装：

```bash
conda install numpy pandas matplotlib requests gymnasium torch stable-baselines3 vaderSentiment python-dotenv -c conda-forge
pip install ib-insync yfinance openai
```

### 2. 配置 API Keys

复制 `.env.example` 为 `.env`，填入 API Keys：

| API | 用途 | 注册地址 |
|-----|------|----------|
| `FINNHUB_API_KEY` | 新闻数据（兜底，60 次/天） | https://finnhub.io/register |
| `ALPHA_VANTAGE_API_KEY` | 新闻数据（主力，25 次/天） | https://www.alphavantage.co/support/#api-key |
| `DEEPSEEK_API_KEY` | **LLM 分析新闻**（默认，必填！） | [DeepSeek 平台](https://platform.deepseek.com/api_keys) |
| `GLM_API_KEY` | LLM 分析（备选） | [智谱 AI 控制台](https://open.bigmodel.cn/dev/api/key) |

---

## 🔧 运行回测

一条命令跑通阶段 1-3（三条路径对比）：

```bash
python scripts/run_backtest.py
```

**默认参数**：
- 候选池：AAPL, NVDA, MSFT, GOOGL, AMZN, JPM, V, JNJ, UNH, WMT
- 时间范围：2025-08-11 ~ 2026-08-09（一年）
- Top-K：5 只，每周调仓

**最新回测结果**（2025-08-11 ~ 2026-08-09，一年期）：

| 指标 | LLM Top-K |
|------|----------|
| 累计收益率 | **+10.00%** |
| 最终权益 | **$110,003** |
| 初始资金 | $100,000 |

> **说明**：禁用置信度校准后，LLM Top-K 策略在一年期回测中实现 10% 稳定收益，结果可复现。


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
├── utils/                     # 工具模块
│   ├── llm_client.py         #   LLM API 统一客户端
│   └── logger.py             #   日志系统（RotatingFile + LLM 审计）
├── scripts/
│   ├── run_backtest.py        # 端到端回测入口（统一脚本）
│   ├── ablation_experiment.py # Stage 4: L1 记忆消融实验
│   ├── clear_llm_cache.py     # 清除 LLM 缓存工具
│   ├── data_fetch_tracker.py  # 新闻数据拉取进度追踪
│   └── consolidate_news_cache.py # 新闻缓存归并工具
├── tests/                     # 测试
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
- ✅ **智能缓存**：基于全局锚点网格的段级缓存（28天段），天然去重、跨回测复用
- ✅ **分段拉取**：自动将长历史切成 28 天一段逐段请求，效率翻倍

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
- [ ] PPO 仓位融合器待验证（阶段 5）
- [ ] IBKR 模拟盘接入（阶段 6）

---

## 📝 Version History

- **v1.4 (2026-08-14)**: 置信度校准机制禁用
  - ❌ 禁用 L-Calibrate 置信度校准（收益从 10% 降至 3.78%，校准方向错误）
  - ✅ 修复 `INDICATOR_WARMUP_DAYS` 从 15 天到 60 天（技术指标需要足够预热期）
  - ✅ 统一回测入口为 `scripts/run_backtest.py`（删除冗余的 stage3/stage5 脚本）
  - ✅ 清理冗余文档和临时文件（output 目录、calibration 分析报告等）
  - ✅ 结果稳定可复现：两次运行都得到 ~10% 收益率

- **v1.3 (2026-08-12)**: 工程化增强
  - ✅ 日志系统：`utils/logger.py`（RotatingFile + 错误分离 + LLM 调用审计 JSONL）
  - ✅ 并行化：LLM 选股分析 ThreadPoolExecutor 并行（线程安全 SQLite + 独立 Agent）
  - ✅ 调仓日历锚定：`REBALANCE_WEEKLY` ISO 周切换，跨回测 LLM 缓存复用
  - ✅ 新闻缓存优化：cache_hit 追踪，命中时跳过 15s sleep
  - ✅ 默认 LLM 切换为 DeepSeek（`deepseek-v4-flash`）
  - ✅ 段大小 14→28 天，AV 配额利用率翻倍

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
