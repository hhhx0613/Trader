"""
全局配置文件
所有可调参数集中在此处管理，方便后续修改和实验对比。
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

# 回测输出目录（净值曲线、交易记录、评估报告）
OUTPUT_DIR = PROJECT_ROOT / "output"

# 确保目录存在
MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ==================== 数据源配置 ====================

# 默认交易标的（美股苹果，数据稳定、适合入门演示）
DEFAULT_SYMBOL = "NVDA"

# 默认回测时间范围（最近一个月，跑通流程）
DEFAULT_START_DATE = "2026-07-06"
DEFAULT_END_DATE = "2026-08-06"

# Alpha Vantage API Key（免费申请：https://www.alphavantage.co/support/#api-key）
# 支持多个 key 用逗号分隔，自动轮换以翻倍配额（如 "KEY1,KEY2"）
_AV_KEYS_RAW = os.getenv("ALPHA_VANTAGE_API_KEY", "")
ALPHA_VANTAGE_API_KEYS = [k.strip() for k in _AV_KEYS_RAW.split(",") if k.strip()]
# 兼容旧代码：第一个 key 作为默认
ALPHA_VANTAGE_API_KEY = ALPHA_VANTAGE_API_KEYS[0] if ALPHA_VANTAGE_API_KEYS else ""

# key 轮换计数器（每次调用 get_next_av_key 自动切换）
_av_key_idx = 0

def get_next_av_key() -> str:
    """获取下一个 Alpha Vantage API key（round-robin 轮换）。"""
    global _av_key_idx
    if not ALPHA_VANTAGE_API_KEYS:
        return ""
    key = ALPHA_VANTAGE_API_KEYS[_av_key_idx % len(ALPHA_VANTAGE_API_KEYS)]
    _av_key_idx += 1
    return key

# IBKR TWS/IB Gateway API 端口（IB Gateway 模拟盘默认 4002）
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))

# 数据源优先级：本地缓存 → yfinance → Alpha Vantage → IBKR
# 无需额外配置，data_collector 内部自动按此顺序兜底

# 新闻多段请求（PPO 训练时需要更长的新闻历史，打开此开关）
# AV News API 单次最多返回 1000 条（约覆盖 14-20 天），
# 开启后会将日期范围切成多段分别请求再合并，消耗更多 API 配额。
NEWS_MULTI_SEGMENT = True
NEWS_SEGMENT_DAYS = 14  # 每段覆盖的天数（AV 1000 条 ≈ 14 天高产量股票）
NEWS_DAILY_QUOTA = 25   # 单次运行最多消耗的新闻 API 次数（AV 免费版 25 次/天，按 IP 限额）


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
DEFAULT_LLM_PROVIDER = "glm"

# 默认 LLM 模型名称（为空时使用提供商的默认模型）
DEFAULT_LLM_MODEL = "glm-4"
