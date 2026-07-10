"""
技术指标计算模块（Indicators）

职责：
  基于标准化 OHLCV DataFrame，计算常用技术分析指标。
  所有函数都接收 DataFrame，返回新增列的 DataFrame（不修改原始数据）。

阶段1 实现的指标：
  - MA（简单移动平均线）：趋势方向判断的基础
  - EMA（指数移动平均线）：对近期价格更敏感，减少滞后
  - RSI（相对强弱指数）：超买/超卖判断
  - MACD（指数平滑异同移动平均线）：趋势强度与方向
  - ATR（平均真实波幅）：衡量价格波动性，用于止损/仓位管理
  - VWAP（成交量加权平均价）：成交量加权的公允价格参考
  - ADX（平均趋向指标）：衡量趋势强度，用于自适应切换策略权重

为什么不用 ta 库（技术分析库）：
  - 阶段1只需要 3 个基础指标，用 pandas 手写即可，不引入额外依赖
  - 手写实现有助于理解算法原理，论文中可以展开说明
  - 阶段4的因子Agent会引入 ta 库做 25+ 维因子批量计算
"""

import pandas as pd
import numpy as np
from typing import Tuple

import config


# ==================== MA（简单移动平均线）====================

def compute_ma(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算短期和长期简单移动平均线（SMA）。

    金叉/死叉策略的核心依据：
      - 短期均线上穿长期均线 → 金叉（看涨信号）
      - 短期均线下穿长期均线 → 死叉（看跌信号）

    参数读取 config.MA_SHORT / config.MA_LONG，默认 5 / 20。

    返回：
      在原 DataFrame 基础上新增 ma_short, ma_long 两列的副本。
    """
    result = df.copy()

    # rolling(window).mean() 是 pandas 计算移动平均的标准方法
    # min_periods=1 保证前几行（数据不足一个窗口时）也能计算出值，而非 NaN
    result["ma_short"] = result["close"].rolling(
        window=config.MA_SHORT, min_periods=1
    ).mean()

    result["ma_long"] = result["close"].rolling(
        window=config.MA_LONG, min_periods=1
    ).mean()

    return result


# ==================== EMA（指数移动平均线）====================

def compute_ema(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算短期和长期指数移动平均线（EMA）。

    EMA 与 SMA 的区别：
      - SMA 对窗口内所有数据等权平均
      - EMA 对近期数据赋予更高权重，对价格变化反应更敏感
      - 权重衰减由 span 参数控制：alpha = 2 / (span + 1)

    交易用法：
      - EMA 短期上穿 EMA 长期 → 看涨
      - 价格位于 EMA 上方 → 上升趋势

    参数读取 config.EMA_SHORT(9) / config.EMA_LONG(21)。
    """
    result = df.copy()
    result["ema_short"] = result["close"].ewm(span=config.EMA_SHORT, adjust=False).mean()
    result["ema_long"] = result["close"].ewm(span=config.EMA_LONG, adjust=False).mean()
    return result


# ==================== RSI（相对强弱指数）====================

def compute_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 RSI（Relative Strength Index，相对强弱指数）。

    RSI 衡量一段时间内价格上涨与下跌的相对强度：
      - RSI > 70 → 超买区间，可能回调（卖出信号）
      - RSI < 30 → 超卖区间，可能反弹（买入信号）
      - RSI 在 30~70 之间 → 正常区间

    计算公式（Wilder 平滑法）：
      1. 计算每日涨跌幅 change = close - close_prev
      2. 分离涨幅 gain 和跌幅 loss（负值取 0）
      3. 用 Wilder 指数平滑：avg_gain = (prev_avg_gain * (n-1) + current_gain) / n
      4. RS = avg_gain / avg_loss
      5. RSI = 100 - 100 / (1 + RS)

    这里使用 pandas 的 ewm（指数加权移动平均）实现 Wilder 平滑，
    alpha = 1/period 等价于 Wilder 的递推公式。
    """
    result = df.copy()
    period = config.RSI_PERIOD

    # 第 1 步：计算每日价格变化
    delta = result["close"].diff()  # diff() = close[t] - close[t-1]

    # 第 2 步：分离涨幅和跌幅
    # gain：只保留上涨部分，下跌记为 0
    gain = delta.where(delta > 0, 0.0)
    # loss：只保留下跌部分，取绝对值（方便后续计算）
    loss = (-delta).where(delta < 0, 0.0)

    # 第 3 步：Wilder 指数平滑（用 ewm 实现）
    # alpha=1/period 是 Wilder 原始公式的等价写法
    # adjust=False 使用递推形式，与教科书公式一致
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # 第 4 步：计算 RS 和 RSI
    # 当 avg_loss 为 0（连续上涨无下跌）时，RS → ∞，RSI = 100
    rs = avg_gain / avg_loss.replace(0, np.nan)  # 除以 0 替换为 NaN
    result["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    # 填充 NaN：前 period 行因数据不足无法计算 RSI，填充为 50（中性值）
    result["rsi"] = result["rsi"].fillna(50.0)

    return result


# ==================== MACD（指数平滑异同移动平均线）====================

def compute_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 MACD 指标（Moving Average Convergence Divergence）。

    MACD 由三部分组成：
      - MACD Line（快线）：短期 EMA - 长期 EMA
      - Signal Line（慢线）：MACD Line 的 EMA
      - Histogram（柱状图）：MACD Line - Signal Line

    交易信号：
      - MACD 上穿 Signal Line → 看涨（金叉）
      - MACD 下穿 Signal Line → 看跌（死叉）
      - Histogram 由负转正 → 动量增强

    参数读取 config.MACD_FAST(12) / MACD_SLOW(26) / MACD_SIGNAL(9)。
    """
    result = df.copy()

    # EMA（指数移动平均线）：
    # span=N 的 EMA 等价于 alpha = 2/(N+1) 的指数加权平均
    ema_fast = result["close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = result["close"].ewm(span=config.MACD_SLOW, adjust=False).mean()

    # MACD Line = 快线 EMA - 慢线 EMA
    result["macd_line"] = ema_fast - ema_slow

    # Signal Line = MACD Line 的 EMA
    result["macd_signal"] = result["macd_line"].ewm(
        span=config.MACD_SIGNAL, adjust=False
    ).mean()

    # Histogram = MACD Line - Signal Line（柱状图，直观显示多空力量对比）
    result["macd_hist"] = result["macd_line"] - result["macd_signal"]

    return result


# ==================== ATR（平均真实波幅）====================

def compute_atr(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 ATR（Average True Range，平均真实波幅）。

    ATR 衡量价格的实际波动幅度，与方向无关：
      - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
      - ATR = TR 的 Wilder 指数平滑均值

    用途：
      - 波动性衡量：ATR 越大，价格波动越剧烈
      - 动态止损：常用 2xATR 或 3xATR 作为止损距离
      - 仓位管理：波动大时减小仓位，波动小时加大仓位

    参数读取 config.ATR_PERIOD(14)。
    """
    result = df.copy()
    period = config.ATR_PERIOD

    # True Range：三种价格波动的最大值
    high_low = result["high"] - result["low"]
    high_close = (result["high"] - result["close"].shift(1)).abs()
    low_close = (result["low"] - result["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

    # Wilder 平滑（与 RSI 相同的方法）
    result["atr"] = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # 前几行数据不足时填充为 TR 本身
    result["atr"] = result["atr"].fillna(tr)

    return result


# ==================== VWAP（成交量加权平均价）====================

def compute_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 VWAP（Volume Weighted Average Price，成交量加权平均价）。

    VWAP = 累积(价格 * 成交量) / 累积(成交量)
    其中价格使用典型价格：(high + low + close) / 3

    意义：
      - 代表市场参与者的平均持仓成本
      - 价格在 VWAP 上方 → 多头占优
      - 价格在 VWAP 下方 → 空头占优
      - 机构常用 VWAP 作为交易基准（低于 VWAP 买入为“优质”）

    注意：日线 VWAP 是累积计算，每个新周期重新开始。
    这里简化为整个区间的累积 VWAP，适用于回测场景。
    """
    result = df.copy()

    # 典型价格 = (最高 + 最低 + 收盘) / 3
    typical_price = (result["high"] + result["low"] + result["close"]) / 3

    # 累积计算
    tp_volume = typical_price * result["volume"]
    result["vwap"] = tp_volume.cumsum() / result["volume"].cumsum()

    return result


# ==================== ADX（平均趋向指标）====================

def compute_adx(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 ADX（Average Directional Index，平均趋向指标）。

    ADX 衡量趋势的强度（与方向无关）：
      - ADX > 25 → 强趋势（上涨或下跌）
      - ADX < 20 → 弱趋势或震荡市
      - 20~25 → 过渡区域

    计算流程：
      1. +DM = high - prev_high（仅当 > 0 且 > -DM 时保留）
      2. -DM = prev_low - low（仅当 > 0 且 > +DM 时保留）
      3. +DI = 100 * smooth(+DM) / ATR
      4. -DI = 100 * smooth(-DM) / ATR
      5. DX = 100 * |+DI - -DI| / (+DI + -DI)
      6. ADX = DX 的 Wilder 平滑均值

    用途：
      - 市场状态识别：趋势行情 vs 震荡行情
      - 策略权重切换：趋势行情用均线/MACD，震荡行情用 RSI

    参数读取 config.ADX_PERIOD(14)。
    """
    result = df.copy()
    period = config.ADX_PERIOD

    # 第 1 步：计算 +DM 和 -DM
    high_diff = result["high"].diff()
    low_diff = -result["low"].diff()  # prev_low - low = -(low - prev_low)

    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)

    plus_dm = pd.Series(plus_dm, index=result.index)
    minus_dm = pd.Series(minus_dm, index=result.index)

    # 第 2 步：Wilder 平滑
    smooth_plus = plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smooth_minus = minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # 第 3 步：用 ATR 归一化得到 +DI 和 -DI
    atr = result["atr"] if "atr" in result.columns else \
        pd.concat([
            result["high"] - result["low"],
            (result["high"] - result["close"].shift(1)).abs(),
            (result["low"] - result["close"].shift(1)).abs()
        ], axis=1).max(axis=1).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    plus_di = 100.0 * smooth_plus / atr.replace(0, np.nan)
    minus_di = 100.0 * smooth_minus / atr.replace(0, np.nan)

    # 第 4 步：计算 DX 和 ADX
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = 100.0 * di_diff / di_sum.replace(0, np.nan)

    result["adx"] = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    result["adx"] = result["adx"].fillna(0.0)

    return result


# ==================== 统一计算入口 ====================

def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    一次性计算所有技术指标，返回完整 DataFrame。

    这是回测引擎和策略模块调用的主入口。
    设计为链式调用，每个函数返回新 DataFrame，不修改输入数据。

    使用方式：
        df = data_collector.fetch_ohlcv("AAPL")
        df = compute_all_indicators(df)
        # 现在 df 包含：OHLCV + ma/ema/rsi/macd/atr/vwap 等指标列
    """
    df = compute_ma(df)
    df = compute_ema(df)
    df = compute_rsi(df)
    df = compute_macd(df)
    df = compute_atr(df)
    df = compute_vwap(df)
    df = compute_adx(df)
    return df
