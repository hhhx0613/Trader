# 量化交易 LLM 多 Agent 系统毕设

> **一句话总结**：LLM 分析新闻做选股，PPO 训练总仓位控制，规则基线作为对照。  
> **当前状态**：✅ 阶段 0-3 全部完成（无 PPO，纯规则 +LLM 决策验证）

---

## 📋 核心架构

```
┌─────────────────────────────────────┐
│   Stage 4-7 (未来) ⏸️                │
│   - PPO 强化学习                    │
│   - 多 Agent 协同                   │
│   - IBKR 模拟盘                      │
└─────────────────────────────────────┘
                ↓
┌─────────────────────────────────────┐
│   Stage 1-3 ✅ COMPLETED              │
│   ┌───┐  ┌──────┐  ┌──────────┐     │
│   │数  │→│情绪/决策 │ →│回测撮合  │     │
│   │据  │  │(LLM/VADER)│ │           │
│   └───┘  └──────┘  └──────────┘     │
│  规则基线 / LLM Top-K / VADER Top-K    │
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
| `FINNHUB_API_KEY` | 新闻数据（近期 7 天免费） | https://finnhub.io/register |
| `ALPHA_VANTAGE_API_KEY` | 新闻数据（历史，1000 条免费）+OHLCV | https://www.alphavantage.co/support/#api-key |
| `GLM_API_KEY` | **Llm 分析新闻**（必填！） | [智谱 AI 控制台](https://open.bigmodel.cn/dev/api/key) |

---

## 🔧 运行测试（端到端）

一条命令跑通阶段 1-3：

```bash
python scripts/run_backtest_stage3.py --pool AAPL NVDA --start 2026-07-07 --end 2026-08-06
```

**输出对比表**（三条路径）：

| 路径 | 累计收益率 | 最大回撤 | 交易次数 |
|------|-----------|---------|---------|
| 规则基线 | 0.00% | 0.00% | 0 |
| LLM Top-K | **-1.78%** | 4.29% | 2 |
| VADER Top-K | **-0.06%** | 3.30% | 4 |
| Buy&Hold (AAPL) | +0.11% | 10.78% | - |

> **说明**：VADER 表现最好（最大回撤仅 3.3%，低于 AAPL 基准的 10.78%）。

---

## 📂 文件结构（按 Review 顺序）

### 第 1 步：数据管道（Stage 1）

```
core/config.py               # ← 全局配置（API key、时间范围、参数）
core/data/market_data.py     # OHLCV 下载（yfinance / AV 兜底）
core/data/news_data.py       # 新闻下载（AV 优先 +Finnhub 兜底，分段模式可打开）
core/indicators.py           # 技术指标计算（MA/RSI/MACD/VWAP/ADX）
```

**关键机制**：
- ✅ Point-in-Time：缓存命中后才请求 API，防止前视偏差
- ✅ 三层兜底：本地缓存 → yfinance → Alpha Vantage
- ✅ 新闻合并：AV News（1000 条历史）+Fin-hub（最近 7 天），去重后使用

---

### 第 2 步：情绪 + 决策（Stage 2）

```
agents/llm_analyst.py        # GLM-4 LLM 分析新闻（带缓存，不重复调用）
agents/stock_selector.py     # Top-K 选股逻辑（持有优先 + 置信度过滤）
agents/decision_func.py      # 决策函数（LLM /VADER 双路径）
core/data/sentiment.py       # VADER 情绪打分（备用方案，不依赖 LLM）
```

**关键机制**：
- ✅ 缓存机制：`data/cache/llm/*.json`，避免重复消费 API 配额
- ✅ Top-K 等权 + 持有优先：选出的股票等权分配，已在持仓的不卖
- ✅ 技术指标调整：RSI 超买 → 置信度下降 → 仓位缩减

---

### 第 3 步：回测 + 风控（Stage 3）

```
core/base_engine.py          # ← 公共基类（买卖撮合 + 风控集成）
core/backtest_engine.py      # 单股回测（main.py 用）
core/multi_stock_engine.py   # 多股回测（run_backtest_stage3.py 用）
core/risk_manager.py         # 风控硬规则（止损/回撤/连亏降仓 R4）
core/recorder.py             # 交易记录 + 绩效评估
scripts/run_backtest_stage3.py # ← 端到端测试入口
```

**关键机制**：
- ✅ 防前视偏差：T-1 收盘生成信号 → T 开盘撮合执行
- ✅ R4 连亏降仓：每笔亏损自动调用 `record_trade_result(False)`
- ✅ 公式仓位：exposure = avg_confidence × MAX_EXPOSURE_RATIO
- ✅ 滑点 + 手续费：统一在基类 `buy()` / `sell()` 中扣除

---

## 🎯 阶段功能验收清单

### ✅ 阶段 0/1 规则基线

- [x] `main.py --symbol AAPL --start ... --end ...`
- [x] MA/RSI/MACD 多条件投票生成信号
- [x] BacktestEngine 逐 Bar 撮合
- [x] Recorder 输出报告 + 绘制净值曲线

---

### ✅ 阶段 2 LLM Analyst Agent

- [x] `tests/test_stage2.py` 跑通
- [x] LLM 分析新闻返回 direction/confidence/reasons
- [x] Top-K 选股 + 持有优先
- [x] Cache 在 `data/cache/llm/`

---

### ✅ 阶段 3 最简决策链闭环

- [x] `scripts/run_backtest_stage3.py` 跑通
- [x] 5 次调仓全触发（每周一次）
- [x] RSI 超买降仓位 → 止损触发平仓
- [x] R4 连亏降仓被调用
- [x] 规则/Llm/Vader 三条路径同时跑

---

## 🔧 高级配置（PPO 预备）

### 多段请求拼接长历史新闻

PPO 训练时需要更多历史数据，修改 `config.py`：

```python
NEWS_MULTI_SEGMENT = True   # 打开此开关
NEWS_SEGMENT_DAYS = 14      # 每段天数（AV 1000 条 ≈ 14 天）
```

会自动把 3 年数据切成 ~78 段分别请求（跨多天拉完），已拉取段有独立缓存。

---

## 📊 已知问题与待办

- [ ] R5 换手惩罚死代码（`record_turnover()` 未被调用）
- [ ] evaluate() 缺少换手率指标（Plan.md 要求但代码未实现）
- [ ] PPO 环境（gym.Env）尚未实现
- [ ] IBKR 模拟盘接入（阶段 5）

---

## 📝 Version History

- **v1.0 (2026-08-06)**: 阶段 0-3 全部完成
  - 重构：抽取 `BaseBacktestEngine` 消除重复代码
  - 修复：`rebalance_counter` 卡死 bug、空 DataFrame 崩溃
  - 新增：端到端验证脚本、LLM/VADER 双路径
  - 优化：新闻源优先级从合并改为"AV 优先+Finnhub 兜底”
