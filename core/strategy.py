"""
规则式交易策略模块（Strategy）

职责：
  根据技术指标生成交易信号（买入/卖出/持有）。
  阶段1 使用纯规则策略，不需要任何机器学习训练。

策略逻辑（自适应多条件融合投票）：
  综合 EMA趋势、RSI超买超卖、MACD方向 三个维度的信号，
  根据 ADX 识别市场状态，动态调整各指标权重：
  - 趋势行情（ADX > 25）：趋势指标（EMA/MACD）权重高，RSI 权重低
  - 震荡行情（ADX ≤ 25）：RSI 权重高，趋势指标权重低
  同时用 VWAP 作为买入过滤条件（价格在 VWAP 上方才允许买入）。

为什么用多条件投票而非单一指标：
  - 单一指标容易产生假信号（如震荡市中均线频繁金叉死叉）
  - 多条件共振可以过滤噪声，提高信号质量
  - 这也是论文中对照组的设计基础：后续 PPO 策略需要比这个规则策略更好才有意义

设计约束（防止前视偏差）：
  策略在时间步 t 只能使用 t 及之前的数据（包括 t 时刻的收盘价和指标值），
  绝对不能使用 t+1 及之后的数据。这是回测系统最基本的正确性要求。
"""

import pandas as pd
import numpy as np

from . import config


# ==================== 公开接口 ====================

def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    根据技术指标生成交易信号序列。

    输入：
      包含技术指标列的 DataFrame（需先调用 indicators.compute_all_indicators）

    输出：
      在原 DataFrame 基础上新增 signal 列的副本：
        signal =  1  → 买入信号
        signal = -1  → 卖出信号
        signal =  0  → 无信号（持仓不动）

    信号生成规则（自适应投票）：
      ┌──────────┬─────────────────────┬──────────────────────┬────────────┐
      │ 指标     │ 看涨条件 (+1)       │ 看跌条件 (-1)        │ 趋势权重   │
      ├──────────┼─────────────────────┼──────────────────────┼────────────┤
      │ EMA      │ 短期EMA > 长期EMA   │ 短期EMA < 长期EMA    │ 趋势:1.5 震荡:0.5 │
      │ RSI      │ RSI < 超卖阈值      │ RSI > 超买阈值       │ 趋势:0.5 震荡:1.5 │
      │ MACD     │ MACD柱 > 0          │ MACD柱 < 0           │ 趋势:1.5 震荡:0.5 │
      └──────────┴─────────────────────┴──────────────────────┴────────────┘

      市场状态由 ADX 判断：ADX > 25 为趋势行情，否则为震荡行情
      买入：加权总分 > 阈值，且价格 > VWAP（趋势过滤）
      卖出：加权总分 < -阈值，或 RSI 严重超买(>80)
    """
    result = df.copy()

    # ---------- 第 1 步：逐指标打分 ----------

    # EMA 趋势得分：短期均线在长期均线之上 = +1，之下 = -1
    ema_score = np.where(
        result["ema_short"] > result["ema_long"], 1,
        np.where(result["ema_short"] < result["ema_long"], -1, 0)
    )

    # RSI 得分：超卖区 = +1（看涨反弹），超买区 = -1（看跌回调）
    rsi_score = np.where(
        result["rsi"] < config.RSI_OVERSOLD, 1,
        np.where(result["rsi"] > config.RSI_OVERBOUGHT, -1, 0)
    )

    # MACD 得分：柱状图为正 = +1（多头动量），为负 = -1（空头动量）
    macd_score = np.where(
        result["macd_hist"] > 0, 1,
        np.where(result["macd_hist"] < 0, -1, 0)
    )

    # ---------- 第 2 步：ADX 自适应权重 ----------

    # 判断市场状态：ADX > 阈值 → 趋势行情，否则 → 震荡行情
    is_trending = result["adx"] > config.ADX_TREND_THRESHOLD

    # 趋势行情权重：趋势指标 1.5，RSI 0.5
    # 震荡行情权重：趋势指标 0.5，RSI 1.5
    trend_w = np.where(is_trending, 1.5, 0.5)
    rsi_w = np.where(is_trending, 0.5, 1.5)

    # 加权总分（范围约：-4.5 ~ +4.5）
    total_score = (
        ema_score * trend_w +
        rsi_score * rsi_w +
        macd_score * trend_w
    )

    # ---------- 第 3 步：生成信号 ----------

    # VWAP 过滤：价格在 VWAP 上方才允许买入（确认多头趋势）
    above_vwap = result["close"] > result["vwap"]

    # 买入条件：加权总分 >= 2 且价格 > VWAP
    buy_condition = (total_score >= 2) & above_vwap

    # 卖出条件：加权总分 <= -2，或 RSI 严重超买（>80，强制止盈）
    sell_condition = (total_score <= -2) | (result["rsi"] > 80)

    result["signal"] = 0  # 默认持有
    result.loc[buy_condition, "signal"] = config.SIGNAL_BUY
    result.loc[sell_condition, "signal"] = config.SIGNAL_SELL

    # 注意：信号去重（连续买入/卖出信号只执行一次）由回测引擎处理
    # 回测引擎通过检查当前持仓状态决定是否执行信号，策略层只做纯籹信号生成

    return result


# ==================== 辅助函数 ====================

def get_signal_at(df: pd.DataFrame, index: int) -> int:
    """
    获取指定行（时间步）的交易信号。

    回测引擎在逐行遍历时调用此函数获取当前 Bar 的信号。

    参数：
      df    : 已生成信号的 DataFrame
      index : 行索引（整数位置，0 = 第一天）

    返回：
      config.SIGNAL_BUY / SIGNAL_SELL / SIGNAL_HOLD
    """
    return int(df.iloc[index]["signal"])
