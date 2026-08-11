# 公开 API 接口文档

> **最后更新：** 2026-08-07  
> **维护者：** Trader 项目团队  
> **用途：** 记录项目对外暴露的公开接口，供外部调用和 review

---

## 📋 目录

1. [行情数据接口](#1-行情数据接口)
   - [fetch_ohlcv](#11-fetch_ohlcv)
2. [新闻数据接口](#2-新闻数据接口)
   - [fetch_news](#21-fetch_news)
   - [get_news_at_date](#22-get_news_at_date)
3. [情绪分析接口](#3-情绪分析接口)
   - [SentimentAnalyzer](#31-sentimentanalyzer)
4. [技术指标接口](#4-技术指标接口)
   - [compute_all_indicators](#41-compute_all_indicators)
5. [策略信号接口](#5-策略信号接口)
   - [generate_signals](#51-generate_signals)
6. [回测引擎接口](#6-回测引擎接口)
   - [BacktestEngine](#61-backtestengine)
   - [MultiStockBacktestEngine](#62-multistockbacktestengine)
7. [LLM 客户端接口](#7-llm-客户端接口)
   - [LLMClient](#71-llmclient)
8. [LLM 分析接口](#8-llm-分析接口)
   - [LLMAnalystAgent](#81-llmanalystagent)
   - [analyze_stock_news](#82-analyze_stock_news)
9. [缓存管理接口](#9-缓存管理接口)
   - [LLMCacheDB](#91-llmcachedb)

---

## 1. 行情数据接口

### 1.1 fetch_ohlcv

**功能：** 获取指定股票的日线 OHLCV 数据（自动多层兜底）

**位置：** `core/data/market_data.py`

**函数签名：**
```python
def fetch_ohlcv(
    symbol: str = config.DEFAULT_SYMBOL,
    start_date: str = config.DEFAULT_START_DATE,
    end_date: str = config.DEFAULT_END_DATE,
) -> pd.DataFrame
```

**参数说明：**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `symbol` | str | `"NVDA"` | 股票代码（如 `"AAPL"`, `"TSLA"`） |
| `start_date` | str | `"2023-07-01"` | 起始日期，格式 `"YYYY-MM-DD"` |
| `end_date` | str | `"2026-07-01"` | 结束日期，格式 `"YYYY-MM-DD"` |

**返回值：**
- `pd.DataFrame`，包含以下列：
  - `open`: 开盘价 (float)
  - `high`: 最高价 (float)
  - `low`: 最低价 (float)
  - `close`: 收盘价 (float)
  - `volume`: 成交量 (int)
- `index`: `DatetimeIndex` (tz-naive UTC)

**数据源优先级：**
1. 本地 CSV 缓存（最快）
2. yfinance（主力免费源）
3. Alpha Vantage（备用）
4. IBKR API（最终兜底）

**使用示例：**
```python
from core.data.market_data import fetch_ohlcv

# 获取 NVDA 近 1 年数据
df = fetch_ohlcv("NVDA", "2025-08-07", "2026-08-06")
print(f"获取到 {len(df)} 条记录")
```

---

## 2. 新闻数据接口

### 2.1 fetch_news

**功能：** 获取指定股票的历史新闻数据（自动多层兜底）

**位置：** `core/data/news_data.py`

**函数签名：**
```python
def fetch_news(
    symbol: str = config.DEFAULT_SYMBOL,
    start_date: str = config.DEFAULT_START_DATE,
    end_date: str = config.DEFAULT_END_DATE,
    use_cache: bool = True,
) -> pd.DataFrame
```

**参数说明：**
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `symbol` | str | `"NVDA"` | 股票代码 |
| `start_date` | str | `"2023-07-01"` | 起始日期 |
| `end_date` | str | `"2026-07-01"` | 结束日期 |
| `use_cache` | bool | `True` | 是否使用本地缓存 |

**返回值：**
- `pd.DataFrame`，包含以下列：
  - `datetime`: 新闻发布时间 (`pd.Timestamp`)
  - `title`: 新闻标题 (str)
  - `summary`: 新闻摘要 (str)
  - `source`: 新闻来源 (str)
  - `sentiment_score`: 情绪分数 (float, -1.0 ~ 1.0)
  - `symbol`: 股票代码 (str)

**数据源优先级：**
1. 本地 CSV 缓存
2. Alpha Vantage（优先，自带情绪分）
3. Finnhub（兜底，无情绪分）

**使用示例：**
```python
from core.data.news_data import fetch_news

# 获取 NVDA 近 1 年新闻
news_df = fetch_news("NVDA", "2025-08-07", "2026-08-06")
print(f"获取到 {len(news_df)} 条新闻")
```

---

### 2.2 get_news_at_date

**功能：** 获取指定股票在指定日期的新闻列表（严格时点对齐，防前视偏差）

**位置：** `core/data/news_data.py`

**函数签名：**
```python
def get_news_at_date(
    news_df: pd.DataFrame,
    symbol: str,
    target_date: str,
) -> List[Dict]
```

**参数说明：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `news_df` | pd.DataFrame | 由 `fetch_news()` 返回的新闻 DataFrame |
| `symbol` | str | 股票代码（用于过滤） |
| `target_date` | str | 目标日期，格式 `"YYYY-MM-DD"` |

**返回值：**
- `List[Dict]`，每个 dict 包含：
  ```python
  {
      "datetime": pd.Timestamp,
      "title": str,
      "summary": str,
      "source": str,
      "sentiment_score": float
  }
  ```

**时点对齐规则：**
- ✅ 只返回 `datetime <= target_date 23:59:59` 的新闻
- ❌ 不能用 `target_date` 之后的新闻（防未来函数）

**内部过滤逻辑：**
1. 先按 `symbol` 过滤（如果有 `symbol` 列）
2. 再按 `target_date` 过滤时点

**使用示例：**
```python
from core.data.news_data import fetch_news, get_news_at_date

news_df = fetch_news("NVDA", "2025-08-07", "2026-08-06")
# 直接传入 symbol，函数内部会自动过滤
news_list = get_news_at_date(news_df, "NVDA", "2026-01-15")
print(f"2026-01-15 及之前有 {len(news_list)} 条 NVDA 新闻")
```

---

## 3. 情绪分析接口

### 3.1 SentimentAnalyzer

**功能：** 对文本进行情绪打分（-1.0 ~ 1.0）

**位置：** `core/data/sentiment.py`

**类定义：**
```python
class SentimentAnalyzer:
    def __init__(self, method: str = "vader")
    def analyze(self, text: str) -> float
    def analyze_batch(self, texts: List[str]) -> List[float]
```

**参数说明：**
| 方法 | 参数 | 说明 |
|------|------|------|
| `__init__` | `method` | 分析方法：`"vader"`（默认）或 `"simple"` |
| `analyze` | `text` | 待分析文本 |
| `analyze_batch` | `texts` | 文本列表 |

**返回值：**
- `analyze()`: float，情绪分数（-1.0 ~ 1.0）
- `analyze_batch()`: List[float]，每条文本的情绪分数

**使用示例：**
```python
from core.data.sentiment import SentimentAnalyzer

analyzer = SentimentAnalyzer(method="vader")
score = analyzer.analyze("Apple reports strong earnings")
print(f"情绪分数：{score:+.3f}")  # 输出：+0.511
```

---

## 4. 技术指标接口

### 4.1 compute_all_indicators

**功能：** 计算所有技术指标（MA, EMA, RSI, MACD, ATR, ADX, VWAP）

**位置：** `core/indicators.py`

**函数签名：**
```python
def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame
```

**参数说明：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `df` | pd.DataFrame | 包含 OHLCV 列的 DataFrame |

**返回值：**
- `pd.DataFrame`，在原 DataFrame 基础上新增以下列：
  - `ma_short`, `ma_long`: 均线（默认 5/20 日）
  - `ema_short`, `ema_long`: 指数均线（默认 9/21 日）
  - `rsi`: RSI 指标（14 日）
  - `macd`, `macd_signal`, `macd_hist`: MACD 指标（12/26/9）
  - `atr`: ATR 指标（14 日）
  - `adx`: ADX 指标（14 日）
  - `vwap`: VWAP 指标

**使用示例：**
```python
from core.data.market_data import fetch_ohlcv
from core.indicators import compute_all_indicators

df = fetch_ohlcv("NVDA", "2025-08-07", "2026-08-06")
df_with_indicators = compute_all_indicators(df)
print(f"RSI: {df_with_indicators['rsi'].iloc[-1]:.2f}")
```

---

## 5. 策略信号接口

### 5.1 generate_signals

**功能：** 根据技术指标生成交易信号（自适应多条件投票）

**位置：** `core/strategy.py`

**函数签名：**
```python
def generate_signals(df: pd.DataFrame) -> pd.DataFrame
```

**参数说明：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `df` | pd.DataFrame | 包含技术指标的 DataFrame |

**返回值：**
- `pd.DataFrame`，在原 DataFrame 基础上新增 `signal` 列：
  - `signal = 1`: 买入信号
  - `signal = -1`: 卖出信号
  - `signal = 0`: 持有不动

**使用示例：**
```python
from core.data.market_data import fetch_ohlcv
from core.indicators import compute_all_indicators
from core.strategy import generate_signals

df = fetch_ohlcv("NVDA", "2025-08-07", "2026-08-06")
df = compute_all_indicators(df)
df = generate_signals(df)

buy_count = (df["signal"] == 1).sum()
sell_count = (df["signal"] == -1).sum()
print(f"买入信号：{buy_count} 次，卖出信号：{sell_count} 次")
```

---

## 6. 回测引擎接口

### 6.1 BacktestEngine

**功能：** 单股票回测引擎（逐 Bar 撮合）

**位置：** `core/backtest_engine.py`

**类定义：**
```python
class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = None,
        backtest_start_date: str = None
    )
    
    def run(
        self,
        df: pd.DataFrame,
        symbol: str = config.DEFAULT_SYMBOL
    ) -> Recorder
```

**使用示例：**
```python
from core.backtest_engine import BacktestEngine

engine = BacktestEngine(initial_capital=100000)
recorder = engine.run(df_with_signals, symbol="NVDA")
recorder.print_report(df_with_signals)
```

---

### 6.2 MultiStockBacktestEngine

**功能：** 多股票回测引擎（Top-K 等权组合 + 每周调仓）

**位置：** `core/multi_stock_engine.py`

**类定义：**
```python
class MultiStockBacktestEngine:
    def __init__(
        self,
        decide_func: Callable,
        initial_capital: float = None,
        backtest_start_date: str = None
    )
    
    def run(
        self,
        market_data: Dict[str, pd.DataFrame],
        news_df: pd.DataFrame,
        candidate_pool: List[str] = None
    ) -> Recorder
```

**使用示例：**
```python
from core.multi_stock_engine import MultiStockBacktestEngine
from agents.decision_func import decide_formula_llm

engine = MultiStockBacktestEngine(
    decide_func=decide_formula_llm,
    initial_capital=100000
)

recorder = engine.run(
    market_data={"NVDA": nvda_df, "AAPL": aapl_df},
    news_df=combined_news_df,
    candidate_pool=["NVDA", "AAPL", "MSFT"]
)
```

---

## 7. LLM 客户端接口

### 7.1 LLMClient

**功能：** 统一的 LLM 客户端（支持 OpenAI、GLM、DeepSeek）

**位置：** `utils/llm_client.py`

**类定义：**
```python
class LLMClient:
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None)
    
    def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> str
    
    def chat_json(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1000,
    ) -> Dict
```

**支持的提供商：**
| Provider | Base URL | 默认模型 | 可选模型 |
|----------|----------|----------|----------|
| `openai` | https://api.openai.com/v1 | gpt-4 | gpt-4, gpt-3.5-turbo |
| `glm` | https://open.bigmodel.cn/api/paas/v4 | glm-4 | glm-4, glm-3-turbo |
| `deepseek` | https://api.deepseek.com/v1 | deepseek-v4-pro | deepseek-v4-pro, deepseek-v4-flash |

**参数说明：**
| 方法 | 参数 | 说明 |
|------|------|------|
| `__init__` | `provider` | LLM 提供商（"openai", "glm", "deepseek"） |
| `__init__` | `model` | 模型名称（可选，默认使用该提供商默认模型） |
| `chat` | `message` | 用户消息 |
| `chat` | `system_prompt` | 系统提示词（可选） |
| `chat` | `temperature` | 温度参数（0.0-2.0） |
| `chat` | `max_tokens` | 最大返回 token 数 |
| `chat_json` | 同上 | 同上，但返回解析后的 JSON 字典 |

**返回值：**
- `chat()`: str，模型回复的文本内容
- `chat_json()`: Dict，解析后的 JSON 字典（解析失败返回 `{"error": "...", "raw": "..."}`）

**环境变量要求：**
- `OPENAI_API_KEY` - OpenAI API Key
- `GLM_API_KEY` - 智谱 AI API Key
- `DEEPSEEK_API_KEY` - DeepSeek API Key

**使用示例：**
```python
from utils.llm_client import LLMClient

# 使用 GLM
client = LLMClient(provider="glm", model="glm-4")

# 普通对话
response = client.chat("你好")
print(response)

# 要求 JSON 输出
result = client.chat_json(
    message="分析以下新闻：Apple reports strong earnings",
    system_prompt="请输出 JSON 格式：{direction, confidence, reasons}"
)
print(result)
```

---

## 8. LLM 分析接口

### 8.1 LLMAnalystAgent

**功能：** LLM 新闻分析师 Agent

**位置：** `agents/llm_analyst.py`

**类定义：**
```python
class LLMAnalystAgent:
    def __init__(self, provider: str = None, model: str = None)
    
    def analyze(
        self,
        symbol: str,
        date: str,
        news_list: List[Dict]
    ) -> Dict
```

**返回值：**
```python
{
    "symbol": str,
    "direction": "bullish | neutral | bearish",
    "confidence": float,  # 0.0 ~ 1.0
    "reasons": List[str],
    "sources": List[str],
    "analysis_date": str
}
```

**使用示例：**
```python
from agents.llm_analyst import LLMAnalystAgent

agent = LLMAnalystAgent(provider="glm", model="glm-4")
result = agent.analyze("NVDA", "2026-01-15", news_list)
print(f"方向：{result['direction']}，置信度：{result['confidence']}")
```

---

### 8.2 analyze_stock_news

**功能：** 便捷函数，分析某只股票在某天的新闻

**位置：** `agents/llm_analyst.py`

**函数签名：**
```python
def analyze_stock_news(
    symbol: str,
    date: str,
    news_df,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict
```

**使用示例：**
```python
from agents.llm_analyst import analyze_stock_news

result = analyze_stock_news("AAPL", "2025-08-07", news_df)
print(result)
```

---

## 9. 缓存管理接口

### 8.1 LLMCacheDB

**功能：** LLM 分析结果缓存数据库（SQLite）

**位置：** `core/data/llm_cache_db.py`

**类定义：**
```python
class LLMCacheDB:
    def __init__(self, db_path: str = None)
    
    def get_cache(self, symbol: str, date: str, model: str) -> Optional[Dict]
    def save_cache(self, data: Dict) -> bool
    def query_by_symbol(self, symbol: str, limit: int = 100) -> List[Dict]
    def query_by_date_range(self, start_date: str, end_date: str) -> List[Dict]
    def statistics(self) -> List[Dict]
    def count(self) -> int
    def clear_all(self)
```

**使用示例：**
```python
from core.data.llm_cache_db import get_cache_db

cache_db = get_cache_db()

# 保存缓存
cache_db.save_cache({
    "symbol": "AAPL",
    "analysis_date": "2025-08-07",
    "model": "glm-4",
    "direction": "bullish",
    "confidence": 0.75,
    "reasons": ["AI增长强劲"],
    "sources": ["Yahoo Finance"],
    "prompt_version": "v1.0"
})

# 查询缓存
result = cache_db.get_cache("AAPL", "2025-08-07", "glm-4")

# 统计信息
stats = cache_db.statistics()
for stat in stats:
    print(f"{stat['symbol']}: {stat['total']} 次分析")
```

---

## 📝 更新日志

### 2026-08-07
- ✅ 首次归档公开 API 接口
- ✅ 包含行情、新闻、情绪、指标、策略、回测、LLM、缓存共 12 个公开接口
- ✅ 记录实测能力和已知限制
- ✅ 补充 LLMClient 统一客户端接口

---

**注意：** 本文档仅记录对外暴露的公开接口，内部实现细节请参考源代码。
