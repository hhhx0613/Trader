"""
全局配置文件
所有可调参数集中在此处管理，方便后续修改和实验对比。

=== Python 环境配置 ===
请使用 Conda 的 trader 环境运行本项目：

```bash
# 激活环境
conda activate trader

# 运行回测脚本
python scripts/run_backtest.py

# 运行测试
pytest tests/
```

如果未创建 conda 环境，请先执行：
```bash
conda create -n trader python=3.10
conda activate trader
pip install -r requirements.txt
```
"""

import os
from pathlib import Path

# 加载 .env 文件中的环境变量（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==================== 路径配置 ====================

# 项目根目录（config.py 位于项目根）
PROJECT_ROOT = Path(__file__).parent

# 本地数据缓存目录（存放下载的 K 线 CSV）
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
MARKET_CACHE_DIR = CACHE_DIR / "market"
NEWS_CACHE_DIR = CACHE_DIR / "news"
NEWS_CACHE_COMPLETE_DIR = NEWS_CACHE_DIR / "complete"      # 已完成的段（回测用）
NEWS_CACHE_INCOMPLETE_DIR = NEWS_CACHE_DIR / "incomplete"  # 未完成的段（实盘增量用）
# SEC EDGAR 工作缓存（submissions/companyfacts 等 JSON）；审计权威在主线存储
FILINGS_CACHE_DIR = CACHE_DIR / "filings"

# ==================== 主线研究存储 ====================
# 主线数据流：fetch → raw 正文库（按源分库）→ EvidenceCard（账本）→ Agent。
# data/cache/ 下的旧 CSV/JSON 是旧基线工作副本，主线不读取。
RESEARCH_DIR = PROJECT_ROOT / "data" / "research"
RESEARCH_RAW_DIR = RESEARCH_DIR / "raw"          # news.db / market.db / filings.db
RESEARCH_LEDGER_PATH = RESEARCH_DIR / "ledger.db"  # 审计账本（卡片/快照/研究对象）

# 回测输出目录（净值曲线、交易记录、评估报告）
OUTPUT_DIR = PROJECT_ROOT / "output"

# 日志目录
LOG_DIR = PROJECT_ROOT / "logs"

# 日志文件大小（MB）和备份数量
MAX_LOG_SIZE_MB = 20  # 每个日志文件最大 20MB
LOG_BACKUP_COUNT = 10   # 保留 10 个历史版本
DEFAULT_LOG_LEVEL = "DEBUG"  # 默认日志级别 (更详细)

# 确保目录存在
MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_CACHE_COMPLETE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_CACHE_INCOMPLETE_DIR.mkdir(parents=True, exist_ok=True)
FILINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ==================== 数据源配置 ====================

# 默认交易标的（美股苹果，数据稳定、适合入门演示）
DEFAULT_SYMBOL = "NVDA"

# 默认回测时间范围（一年数据）——同时用作 PPO 阶段二（LLM 微调）区间
DEFAULT_START_DATE = "2025-08-11"
DEFAULT_END_DATE = "2026-08-09"

# PPO 阶段一（纯数值主干）训练区间：长历史跨周期，避免在单一 regime 上过拟合。
# 与阶段二区间不相交（阶段一 2015-2022 训练，阶段二 2025-2026 微调）。见 docs/Plan.md。
PPO_STAGE1_START_DATE = "2015-01-01"
PPO_STAGE1_END_DATE = "2022-12-31"

# Alpha Vantage API Key（免费申请：https://www.alphavantage.co/support/#api-key）
# Alpha Vantage API Key（单个 key）
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")

# IBKR TWS/IB Gateway API 端口（IB Gateway 模拟盘默认 4002）
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))

# 数据源优先级：本地缓存 → yfinance → Alpha Vantage → IBKR
# 无需额外配置，data_collector 内部自动按此顺序兜底

# 新闻分段请求（永远启用）
# AV News API 单次最多返回 1000 条，
# 自动将日期范围切成 28 天一段分别请求再合并。
NEWS_SEGMENT_DAYS = 28  # 每段覆盖的天数（28天 ≈ 829条，接近1000限制）
NEWS_DAILY_QUOTA = 25   # 单次运行最多消耗的新闻 API 次数（AV 免费版 25 次/天，按 IP 限额）

# 分段缓存的全局锚点（一个周一）。段边界 = [anchor + 28k, anchor + 28k + 27]，
# 与请求起点无关，保证不同区间/不同时间拉取都落在同一批段文件，天然去重、跨回测复用。
# 修改此值会使旧网格段失效，需重新归并。
NEWS_SEGMENT_ANCHOR = "2020-01-06"


# ==================== 技术指标参数 ====================

# EMA 短期周期（替代原 MA_SHORT，EMA 对近期价格更敏感）
EMA_SHORT = 9

# EMA 长期周期（替代原 MA_LONG）
EMA_LONG = 21

# RSI 周期
RSI_PERIOD = 14

# RSI 超买阈值（高于此值视为超买，触发卖出信号）
RSI_OVERBOUGHT = 70

# RSI 超卖阈值（低于此值视为超卖，触发买入信号）
RSI_OVERSOLD = 30

# MACD 快线周期
MACD_FAST = 12

# MACD 慢线周期
MACD_SLOW = 26

# MACD 信号线周期
MACD_SIGNAL = 9

# ATR 周期
ATR_PERIOD = 14

# ADX 周期
ADX_PERIOD = 14

# ADX 趋势判断阈值（高于此值视为有趋势，低于此值视为震荡）
ADX_TREND_THRESHOLD = 25


# ==================== 回测引擎配置 ====================

# 初始资金（美元）
INITIAL_CAPITAL = 100_000.0

# 每笔交易手续费（固定金额，美元）
COMMISSION_PER_TRADE = 1.0

# 滑点比例（成交价 = 信号价 * (1 ± SLIPPAGE)）
SLIPPAGE = 0.001

# 基准年化无风险利率（用于计算夏普比率，美股常用 4%）
RISK_FREE_RATE = 0.04


# ==================== 选股与调仓配置 ====================

# Top-K 选股：LLM 选出置信度最高的 K 只股票等权持有
TOP_K = 5  # 持有 5 只股票（分散化投资）

# 调仓周期：每 N 个交易日调仓一次（5 ≈ 每周）
REBALANCE_DAYS = 5

# 调仓日锚定模式、
#   True  = 每个自然周（ISO 周）的第一个可交易日调仓
#   False = 旧行为：从 all_dates[0] 起每 REBALANCE_DAYS 个交易日计数调仓
REBALANCE_WEEKLY = True

# 置信度阈值：低于此值的股票不入选，全部低于阈值则空仓
CONFIDENCE_THRESHOLD = 0.5

# 最大仓位暴露上限（公式版：平均 confidence × 此值）
MAX_EXPOSURE_RATIO = 1.0

# 波动率目标（Volatility Targeting）：让组合年化波动率贴近此值
# 业界常用 10%~15%，用于决定总暴露大小
TARGET_VOLATILITY = 0.15

# 回退波动率：当某只股票 ATR=0 或缺失时使用的日波动率兜底值
# 约等于年化 25% 波动，偏保守
FALLBACK_VOLATILITY = 0.016


# ==================== 风控参数 ====================

# 最大仓位比例（占总资金的比例，1.0 = 满仓）
MAX_POSITION_RATIO = 1.0

# 单笔最大亏损比例（占总资金的比例，超过则止损）
MAX_SINGLE_LOSS_RATIO = 0.02  # 2%

# ATR 动态止损配置
USE_ATR_STOP = True           # 是否启用 ATR 动态止损
ATR_STOP_MULTIPLIER = 2.5     # ATR 倍数，常用 2x-3x

# 单日最大回撤比例（超过则当日禁止新开仓）
MAX_DAILY_DRAWDOWN = 0.05  # 5%

# 连续亏损降仓：连亏 N 笔后降低仓位比例
MAX_CONSECUTIVE_LOSSES = 3       # 连亏 3 笔触发降仓
CONSECUTIVE_LOSS_PENALTY = 0.5   # 触发后仓位乘以 0.5

# 换手惩罚：每次调仓产生交易时，扣除一定比例作为惩罚
TURNOVER_PENALTY_RATIO = 0.001   # 每笔调仓交易扣除目标仓位的 0.1%


# ==================== 策略信号定义 ====================

# 信号枚举（整型，方便在回测引擎中直接比较）
SIGNAL_BUY = 1      # 买入信号
SIGNAL_SELL = -1    # 卖出信号
SIGNAL_HOLD = 0     # 持仓不动


# ==================== LLM 配置 ====================

# 默认 LLM 提供商（"glm" / "deepseek" / "openai"）
DEFAULT_LLM_PROVIDER = "deepseek"

# 默认 LLM 模型名称（为空时使用提供商的默认模型）
DEFAULT_LLM_MODEL = "deepseek-flash"

# 单次请求超时秒数（瞬时网络异常的重试交由 OpenAI SDK 原生指数退避处理）
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))

# 超时/连接类异常的最大重试次数（作为 openai.OpenAI(max_retries=...) 的入参）
LLM_NETWORK_RETRIES = int(os.getenv("LLM_NETWORK_RETRIES", "2"))


# ==================== 组合政策（PortfolioPolicy，core/portfolio/ 使用） ====================
# 本块是新确定性组合层的政策默认值，与上方旧基线常量（TARGET_VOLATILITY /
# COMMISSION_PER_TRADE / MAX_POSITION_RATIO 等）并存：旧常量服务于 strategy/run_backtest
# 基线，新常量服务于 core/portfolio/PortfolioPolicy，两者口径不同，勿混用。
# config_version 会写入审计对象，回放时据此识别当时所用规则版本。

POLICY_VERSION = "portfolio-policy-v1-provisional"  # 含 provisional：多数阈值未经真实成交校准

# 持仓约束：单券上限 / 保持带 / 最小开仓 / 最多持仓数
MAX_POSITION_WEIGHT = 0.35
HOLD_BAND = 0.02
SEED_WEIGHT = 0.05
MAX_HOLDINGS = 5

# 行业映射：供行业集中度约束使用
SECTORS = {
    "AAPL": "information_technology",
    "AMZN": "consumer_discretionary",
    "GOOGL": "communication_services",
    "JNJ": "health_care",
    "JPM": "financials",
    "MSFT": "information_technology",
    "NVDA": "information_technology",
    "UNH": "health_care",
    "V": "financials",
    "WMT": "consumer_staples",
}

# 逐票独立权重上限，与单券上限经 min() 组合作为分配器上界。
# 注：当前 $100k 模拟账户 × mega-cap 美股，任何基于 ADV 的校准结果都会被截到 1.0，
# 此字段实际不 binding（真正生效的是 MAX_POSITION_WEIGHT）；保留是为规模敏感性
# 实验（账户 >> $50M 或标的下沉至小盘股）预留执法点，届时按 ADV 校准数值。
LIQUIDITY_CAPS = {s: 0.35 for s in SECTORS}

# 分配器目标函数参数（RiskBudgetAllocator / AllocationConfig）
ALLOCATION = {
    "alpha": 1.0,
    "beta": 1.0,
    "gamma": 0.05,
    "max_turnover": 0.3,
    "max_sector_weight": 0.5,
}

# 波动率目标（VolatilityTarget）；与旧标量 TARGET_VOLATILITY 不同，此为整块字典输入
VOLATILITY_TARGET = {
    "target_volatility": 0.15,
    "min_exposure": 0.0,
    "max_exposure": 1.0,
    "max_exposure_increase": 0.2,
}

# 硬风险投影（RiskLimits）：只向下砍风险的硬约束
RISK_LIMITS = {
    "max_exposure": 1.0,
    "cash_floor": 0.05,
    "max_position_weight": 0.35,
    "max_sector_weight": 0.5,
    "max_turnover": 0.3,
    "max_portfolio_volatility": 0.25,
    "drawdown_lock": 0.05,
}

# 成本门控阈值（CostGate / CostLimits）：判定单笔要不要做
COST_LIMITS = {
    "min_order_weight": 0.01,
    "no_trade_band": 0.005,
    "max_turnover": 0.3,
    "max_participation_rate": 0.1,
    "max_cost_rate": 0.01,
}

# 交易成本模型（CostModel）；当前规模下 impact_coefficient 贡献 < $1，主要为链路完整性保留
COST_MODEL = {
    "commission_per_order": 1.0,
    "half_spread_bps": 10.0,
    "impact_coefficient": 0.1,
}
