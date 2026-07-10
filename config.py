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

# 项目根目录（config.py 所在目录）
PROJECT_ROOT = Path(__file__).parent

# 本地数据缓存目录（存放下载的 K 线 CSV）
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# 回测输出目录（净值曲线、交易记录、评估报告）
OUTPUT_DIR = PROJECT_ROOT / "output"

# 确保目录存在
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ==================== 数据源配置 ====================

# 默认交易标的（美股苹果，数据稳定、适合入门演示）
DEFAULT_SYMBOL = "NVDA"

# 默认回测时间范围（3 年日线，数据量适中）
DEFAULT_START_DATE = "2023-07-01"
DEFAULT_END_DATE = "2026-07-01"

# Alpha Vantage API Key（免费申请：https://www.alphavantage.co/support/#api-key）
# 从 .env 文件或环境变量读取，未设置则跳过该数据源
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")

# IBKR TWS/IB Gateway API 端口（IB Gateway 模拟盘默认 4002）
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))

# 数据源优先级：本地缓存 → yfinance → Alpha Vantage → IBKR
# 无需额外配置，data_collector 内部自动按此顺序兜底


# ==================== 技术指标参数 ====================

# 短期均线周期（用于金叉/死叉判断）
MA_SHORT = 5

# 长期均线周期
MA_LONG = 20

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

# EMA 短期周期
EMA_SHORT = 9

# EMA 长期周期
EMA_LONG = 21

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


# ==================== 风控参数 ====================

# 最大仓位比例（占总资金的比例，1.0 = 满仓）
MAX_POSITION_RATIO = 1.0

# 单笔最大亏损比例（占总资金的比例，超过则止损）
MAX_SINGLE_LOSS_RATIO = 0.02  # 2%

# 单日最大回撤比例（超过则当日禁止新开仓）
MAX_DAILY_DRAWDOWN = 0.05  # 5%


# ==================== 策略信号定义 ====================

# 信号枚举（整型，方便在回测引擎中直接比较）
SIGNAL_BUY = 1      # 买入信号
SIGNAL_SELL = -1    # 卖出信号
SIGNAL_HOLD = 0     # 持仓不动
