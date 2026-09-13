"""
PPO 特征工程（features）

把「行情技术指标 + 账户状态 + LLM 语义信号」转成 PPO 的两路观测：
  - numeric(13)：技术6 + 市场环境4 + 账户3 —— 阶段一主干的唯一输入
  - llm(5)    ：语义信号 —— 阶段一填 0，阶段二走旁路注入

设计原则（见 Plan_v2.md 阶段 6）：
  1. 主干永远只看 13 维数值，两阶段输入分布严格一致 → 杜绝阶段二冻结主干遇到 OOD 输入。
  2. LLM 特征独立成一路、与数值解耦 → 旁路可单独量化其边际贡献（消融红利）。
  3. 组合级聚合：特征对整个候选池/持仓聚合，而非单股——因为 PPO 决策的是「总仓位」。
  4. 归一化到 ~[-1,1]：RL 对小尺度、零均值输入更稳定；tanh 压缩抗离群值。
  5. 数据缺失填中性值（0），禁止模拟兜底（项目规范）。

本模块为纯计算函数，不做任何 I/O；数据获取（读缓存、查 LLM 信号）由调用方 env 负责，
以保证特征逻辑可单元测试、可复现。
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional


# ==================== 特征定义（顺序即观测向量顺序，改动会使已训模型失效）====================

# numeric(13)：阶段一主干输入
NUMERIC_FEATURES = [
    # 技术 6
    "avg_rsi", "avg_macd", "avg_adx", "rsi_extreme", "trend_strength", "momentum_avg",
    # 市场环境 4
    "market_volatility", "market_regime", "rolling_20d_ret", "sector_corr",
    # 账户 3
    "current_exposure", "drawdown", "trade_freq",
]
NUMERIC_DIM = len(NUMERIC_FEATURES)  # 13

# llm(5)：阶段二旁路输入
LLM_FEATURES = ["mean_conf", "max_conf", "mean_signed_score", "max_abs_signed_score", "signal_age"]
LLM_DIM = len(LLM_FEATURES)  # 5

# ==================== 归一化尺度常量（把原始量纲压到 ~[-1,1]）====================
# 取「典型量级」作分母，使 tanh 输入落在敏感区（非常态值才饱和），避免信号被压平。

_MACD_SCALE = 0.02        # macd_hist/close 的典型量级（约 2%）
_MOMENTUM_SCALE = 0.10    # 10 日收益典型量级（约 10%）
_REGIME_SCALE = 0.30      # 60 日收益典型量级（约 30%）
_VOL_SCALE = 0.02         # 日波动率 atr/close 典型量级（约 2%）
_SIGNAL_AGE_MAX = 4.0     # signal_age clip 上限（4 周），超过视为「陈旧到无差别」

# 收益窗口（三个不同尺度，捕捉短/中/长期动量，互不重复）
_MOM_WINDOW = 10          # 短期动量
_RET20_WINDOW = 20        # 中期「市场」收益
_LOOKBACK = 60            # 长期 regime + 相关性窗口


# ==================== 行情特征（技术6 + 市场环境4 = 10 维）====================

def _avg_pairwise_corr(ret_series: Dict[str, pd.Series]) -> float:
    """
    候选股两两日收益相关系数均值 → 反映系统性风险 / 分散化程度。

    为什么有用：高相关 = 齐涨齐跌 = 分散化失效、系统性风险高，PPO 应据此降仓；
    低相关 = 个股独立行情，组合更抗跌。取值 [-1,1]，无需再归一化。
    """
    syms = [s for s, r in ret_series.items() if len(r) >= _RET20_WINDOW]
    if len(syms) < 2:
        return 0.0  # 不足两只，相关性无定义 → 中性
    df = pd.DataFrame({s: ret_series[s] for s in syms})
    corr = df.corr().to_numpy()
    iu = np.triu_indices(len(syms), k=1)  # 上三角（不含对角自相关）
    vals = corr[iu]
    vals = vals[~np.isnan(vals)]
    return float(np.mean(vals)) if len(vals) > 0 else 0.0


def compute_market_features(
    market_data: Dict[str, pd.DataFrame],
    date,
    symbols: List[str],
) -> np.ndarray:
    """
    计算 10 维行情特征（技术6 + 市场环境4），对 symbols 池横截面聚合。

    参数：
      market_data: {symbol: DataFrame(含 compute_all_indicators 的指标列)}
      date:        当前日期（须在个股 index 中，否则该股跳过）
      symbols:     参与聚合的股票（候选池或当前持仓）

    返回：长度 10 的 float32 数组，顺序 = NUMERIC_FEATURES[:10]；全部缺失时返回中性 0 向量。

    只读 date 及之前的数据（iloc[:loc+1]），天然 Point-in-Time 安全，无前视偏差。
    """
    date = pd.Timestamp(date)
    snapshots = []                 # 每只有效股票的指标快照
    ret_series: Dict[str, pd.Series] = {}   # symbol -> 日收益序列（供相关性/中期收益）

    for sym in symbols:
        df = market_data.get(sym)
        if df is None or date not in df.index:
            continue  # 停牌/无数据：跳过（不模拟兜底）
        loc = df.index.get_loc(date)
        bar = df.iloc[loc]
        close = float(bar["close"])
        if close <= 0:
            continue

        win = df.iloc[max(0, loc - _LOOKBACK + 1): loc + 1]["close"]

        def _ret(n: int) -> Optional[float]:
            """n 日收益 = close[-1]/close[-1-n] - 1；数据不足返回 None。"""
            return (win.iloc[-1] / win.iloc[-1 - n] - 1.0) if len(win) >= n + 1 else None

        ema_s = float(bar.get("ema_short", close))
        ema_l = float(bar.get("ema_long", close))
        snapshots.append({
            "rsi": float(bar.get("rsi", 50.0)),
            "macd_norm": float(bar.get("macd_hist", 0.0)) / close,   # 除价格消量纲
            "adx": float(bar.get("adx", 25.0)),
            "atr_norm": float(bar.get("atr", 0.0)) / close,          # 日波动率近似
            "ema_bull": 1.0 if ema_s > ema_l else 0.0,               # 多头排列
            "mom_short": _ret(_MOM_WINDOW),
            "mom_long": _ret(len(win) - 1),                          # 窗口全程收益（≈regime）
        })

        r = win.pct_change().dropna()
        if len(r) >= _RET20_WINDOW:
            ret_series[sym] = r.iloc[-_LOOKBACK:]

    if not snapshots:
        return np.zeros(10, dtype=np.float32)  # 全缺失 → 中性

    # --- 技术 6 ---
    avg_rsi = (float(np.mean([s["rsi"] for s in snapshots])) - 50.0) / 50.0     #  centered → [-1,1]
    avg_macd = float(np.tanh(np.mean([s["macd_norm"] for s in snapshots]) / _MACD_SCALE))
    avg_adx = float(np.clip((np.mean([s["adx"] for s in snapshots]) - 25.0) / 25.0, -1.0, 1.0))  # ADX 中性阈值25 → 零均值 [-1,1]
    rsi_extreme = float(np.mean([1.0 if (s["rsi"] > 70 or s["rsi"] < 30) else 0.0
                                 for s in snapshots]))                            # 超买超卖占比
    trend_strength = float(np.mean([s["ema_bull"] for s in snapshots]))          # 多头占比 [0,1]
    moms = [s["mom_short"] for s in snapshots if s["mom_short"] is not None]
    momentum_avg = float(np.tanh(np.mean(moms) / _MOMENTUM_SCALE)) if moms else 0.0

    # --- 市场环境 4 ---
    market_volatility = float(np.tanh(np.mean([s["atr_norm"] for s in snapshots]) / _VOL_SCALE))
    regimes = [s["mom_long"] for s in snapshots if s["mom_long"] is not None]
    market_regime = float(np.tanh(np.mean(regimes) / _REGIME_SCALE)) if regimes else 0.0
    # rolling_20d_ret：池等权 20 日收益（代表「市场」中期方向），复利累乘
    ret20 = [float((1.0 + r.iloc[-_RET20_WINDOW:]).prod() - 1.0)
             for r in ret_series.values() if len(r) >= _RET20_WINDOW]
    rolling_20d_ret = float(np.tanh(np.mean(ret20) / _MOMENTUM_SCALE)) if ret20 else 0.0
    sector_corr = _avg_pairwise_corr(ret_series)

    return np.array([
        avg_rsi, avg_macd, avg_adx, rsi_extreme, trend_strength, momentum_avg,
        market_volatility, market_regime, rolling_20d_ret, sector_corr,
    ], dtype=np.float32)


# ==================== 账户特征（3 维）====================

def compute_account_features(
    current_exposure: float,
    drawdown: float,
    recent_trades: int,
    trade_window: int = _RET20_WINDOW,
) -> np.ndarray:
    """
    账户状态 3 维，由 env 内部账户状态计算。

    参数：
      current_exposure: 当前总暴露 [0,1]
      drawdown:         当前回撤（从净值峰值，正值，如 0.08 = 回撤 8%）
      recent_trades:    近 trade_window 日的交易笔数（衡量换手活跃度）

    为什么进状态：PPO 需感知「自己现在多重仓、亏了多少、交易多频繁」才能做路径依赖决策
    （如回撤后主动降仓、避免过度交易被成本吃掉）。
    """
    exposure = float(np.clip(current_exposure, 0.0, 1.0))
    dd = float(np.clip(drawdown, 0.0, 1.0))
    # 交易频率归一化：假设单日最多约 1 笔/持仓，window 笔封顶
    freq = float(np.clip(recent_trades / max(trade_window, 1), 0.0, 1.0))
    return np.array([exposure, dd, freq], dtype=np.float32)


def build_numeric_obs(
    market_data: Dict[str, pd.DataFrame],
    date,
    symbols: List[str],
    account: Dict,
) -> np.ndarray:
    """
    拼接 numeric(13) = 行情10 + 账户3。

    参数：
      account: {"current_exposure": float, "drawdown": float, "recent_trades": int}
    """
    mkt = compute_market_features(market_data, date, symbols)
    acc = compute_account_features(
        current_exposure=account.get("current_exposure", 0.0),
        drawdown=account.get("drawdown", 0.0),
        recent_trades=account.get("recent_trades", 0),
    )
    return np.concatenate([mkt, acc]).astype(np.float32)


# ==================== LLM 特征（5 维，纯计算）====================

def compute_llm_features(
    signals: Optional[List[Dict]],
    signal_age_days: Optional[float],
) -> np.ndarray:
    """
    5 维 LLM 语义特征（纯计算；信号获取与前向填充由 env 负责）。

    参数：
      signals: [{"confidence": float∈[0,1], "composite_score": float∈[-1,1]}, ...]
               当前生效的 LLM 信号（已前向填充到最近调仓日）；空/None 表示无信号。
      signal_age_days: 距上次 LLM 信号的天数；None 表示从无信号（视为最陈旧）。

    归一化说明：confidence∈[0,1]、composite_score∈[-1,1]（上游 tanh 输出）本就有界，
    仅 signal_age 需 clip 到 [0,4周] 再除以 4 压到 [0,1]。

    缺失处理（项目规范）：无信号时 confidence/score 填 0（中性），age 填最大（最陈旧），
    禁止用「乐观默认值」模拟——否则会污染阶段二旁路对 LLM 增量价值的判断。
    """
    if signals:
        confs = [float(s.get("confidence") or 0.0) for s in signals]
        scores = [float(s.get("composite_score") or 0.0) for s in signals]
        mean_conf = float(np.mean(confs))
        max_conf = float(np.max(confs))
        mean_signed = float(np.mean(scores))
        max_abs_signed = float(np.max(np.abs(scores)))
    else:
        mean_conf = max_conf = mean_signed = max_abs_signed = 0.0

    if signal_age_days is None:
        signal_age_days = _SIGNAL_AGE_MAX * 7.0  # 无信号 → 视为最陈旧
    age = float(np.clip(signal_age_days / 7.0, 0.0, _SIGNAL_AGE_MAX) / _SIGNAL_AGE_MAX)

    return np.array([mean_conf, max_conf, mean_signed, max_abs_signed, age], dtype=np.float32)


def zeros_llm() -> np.ndarray:
    """阶段一：LLM 槽位全 0（主干不接，仅占位以保持两阶段 obs 结构统一）。"""
    return np.zeros(LLM_DIM, dtype=np.float32)
