"""
决策函数：把 LLM 分析结果转换为交易决策

Plan.md 阶段 3 决策逻辑：
  1. 选股(每周)：LLM 分析候选池 → 按 composite_score × confidence 排序选出 Top-K
     - composite_score（代码从 per_news 确定性计算）：信号方向+强度
     - confidence（LLM 评估的证据质量）：信号可信度
     - 乘积 = 期望信号强度，同时要求方向明确且证据可靠
  2. 仓位：波动率目标定总暴露 + 逆波动率定个股权重（业界标准，无需建模）
     - 总暴露 = TARGET_VOLATILITY / σ_portfolio，截断到 MAX_POSITION_RATIO
     - 个股权重 w_i = (1/σ_i) / Σ(1/σ_j)，σ_i 由 ATR/price 近似
  3. 风控：risk_manager 硬规则（止损、回撤、连亏降仓）
  4. 执行：回测引擎按调仓周期执行

返回格式：
  {
    "holdings": [{"symbol": "AAPL", "weight": 0.5}, ...],  # 持仓列表 + 逆波动率权重
    "exposure": 0.6,  # 总仓位暴露（0~1）
  }
"""

# 直接运行本文件时，把项目根目录加入 sys.path，使 `agents` / `core` 等包可被导入
import os
import sys
if __name__ == "__main__":
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

import math
import pandas as pd
from typing import Dict, List, Set
from agents.stock_selector import select_top_k, DEFAULT_CANDIDATE_POOL
from core import config
from core.data.sentiment import SentimentAnalyzer


def _stock_daily_vol(stock_state: Dict) -> float:
    """
    用 ATR/price 近似该股的日波动率。
    ATR 缺失或为 0 时回退到 FALLBACK_VOLATILITY（约年化 25%）。
    """
    price = stock_state.get("current_price", 0.0)
    atr = stock_state.get("atr", 0.0)
    if price > 0 and atr > 0:
        return atr / price
    return config.FALLBACK_VOLATILITY


def _position_sizing(holdings_result, market_state):
    """
    波动率目标 + 逆波动率加权，业界标准仓位分配。

    返回：
      holdings: [{"symbol": str, "weight": float}, ...]  # 权重 = 逆波动率归一
      exposure: float  # 总暴露，= TARGET_VOL / σ_portfolio，截断到上限
    """
    # 1) 每只股票的日波动率（ATR/price 近似）
    vols = []
    for h in holdings_result:
        vol = _stock_daily_vol(market_state.get(h["symbol"], {}))
        vols.append(vol)

    # 2) 逆波动率加权：让每只股票对组合的风险贡献接近相等
    inv_vols = [1.0 / v for v in vols]
    sum_inv = sum(inv_vols)
    weights = [iv / sum_inv for iv in inv_vols]

    holdings = [
        {"symbol": h["symbol"], "weight": w}
        for h, w in zip(holdings_result, weights)
    ]

    # 3) 组合波动率（等权组合的波动率近似 = Σ w_i × σ_i）
    port_vol = sum(w * v for w, v in zip(weights, vols))

    # 4) 波动率目标定总暴露：σ_target / σ_portfolio
    if port_vol > 0:
        exposure = config.TARGET_VOLATILITY / (port_vol * math.sqrt(252))
    else:
        exposure = 0.0

    # 风控截断：不超过 MAX_POSITION_RATIO
    exposure = min(exposure, config.MAX_POSITION_RATIO)

    return holdings, exposure, weights, vols


def decide_formula_llm(
    date,
    market_state: Dict,
    candidate_pool: List[str] = None,
    news_df=None,
    current_holdings: Set[str] = None,
    market_data: Dict = None,
) -> Dict:
    """
    公式版决策函数：LLM Top-K 选股 + 波动率目标/逆波动率加权仓位

    流程（纯客观，无主观调整）：
      1. LLM 分析候选池 → 按 composite_score × confidence 排序选出 Top-K
      2. 仓位由波动率目标 + 逆波动率加权决定（业界标准，无需建模）

    参数：
      date: 当前日期
      market_state: 市场状态（由 multi_stock_engine 提供，包含各股技术指标）
      candidate_pool: 候选股票池
      news_df: 新闻 DataFrame（包含 symbol 列）
      current_holdings: 当前持有的股票集合（用于持有优先）
      market_data: 可选，行情数据 {symbol: DataFrame}，用于 L1 记忆注入实际收益反馈
                   （由 multi_stock_engine 透传；旧调用链不传则降级为一致性记忆）

    返回：
      {
        "holdings": [{"symbol": "AAPL", "weight": 0.65}, ...],
        "exposure": 0.54,  # 总仓位暴露（0~1）
      }
      空仓时返回 {"holdings": [], "exposure": 0.0}
    """
    if candidate_pool is None:
        candidate_pool = DEFAULT_CANDIDATE_POOL
    if current_holdings is None:
        current_holdings = set()

    # ========== 1. LLM Top-K 选股 ==========
    if news_df is not None and len(news_df) > 0:
        selection = select_top_k(
            date=str(date),
            candidate_pool=candidate_pool,
            news_df=news_df,
            current_holdings=current_holdings,
            top_k=config.TOP_K,
            confidence_threshold=config.CONFIDENCE_THRESHOLD,
            provider=config.DEFAULT_LLM_PROVIDER,
            model=config.DEFAULT_LLM_MODEL,
            market_data=market_data,
        )
    else:
        # 没有新闻数据时，不交易
        print(f"  [决策] {date}: 无新闻数据，选择空仓")
        return {"holdings": [], "exposure": 0.0}

    holdings_result = selection["holdings"]

    if not holdings_result:
        return {"holdings": [], "exposure": 0.0}

    # ========== 2. 仓位：波动率目标 + 逆波动率加权 ==========
    holdings, exposure, weights, vols = _position_sizing(
        holdings_result, market_state
    )

    print(f"  [决策] {date}: "
          f"持仓={[h['symbol'] for h in holdings]}, "
          f"权重={[f'{w:.0%}' for w in weights]}, "
          f"日波动率={[f'{v:.2%}' for v in vols]}, "
          f"暴露={exposure:.0%}")

    return {
        "holdings": holdings,
        "exposure": exposure,
    }


def decide_formula_vader(
    date,
    market_state: Dict,
    candidate_pool: List[str] = None,
    news_df=None,
    current_holdings: Set[str] = None,
    market_data: Dict = None,
) -> Dict:
    """
    VADER 规则版决策函数（对照基线）

    与 decide_formula_llm 接口完全一致，但用 VADER 情绪分代替 LLM 分析。
    用于论文消融实验："LLM vs 规则情绪"对比。

    逻辑：
      1. 对每只候选股，取时点对齐的新闻，用 VADER 打分
      2. 平均情绪分 > 0 且 >= 阈值 → bullish，confidence = 平均情绪分
      3. 按 confidence 排序取 Top-K，等权 + 公式仓位

    参数和返回格式同 decide_formula_llm。
    注：market_data 为保持接口一致而接受，VADER 路径不使用。
    """
    if candidate_pool is None:
        candidate_pool = DEFAULT_CANDIDATE_POOL
    if current_holdings is None:
        current_holdings = set()

    if news_df is None or len(news_df) == 0:
        print(f"  [VADER决策] {date}: 无新闻数据，空仓")
        return {"holdings": [], "exposure": 0.0}

    analyzer = SentimentAnalyzer(method="vader")

    # 对每只候选股计算 VADER 情绪
    stock_scores = []
    for symbol in candidate_pool:
        # 过滤该股的新闻
        if "symbol" in news_df.columns:
            symbol_news = news_df[news_df["symbol"] == symbol]
        else:
            symbol_news = news_df

        # 时点对齐：只取 date 及之前的新闻
        target_dt = pd.Timestamp(str(date).split(' ')[0]) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        timely_news = symbol_news[symbol_news["datetime"] <= target_dt]

        if len(timely_news) == 0:
            avg_sentiment = 0.0
        else:
            # 对标题+摘要打分，取平均
            scores = []
            for _, row in timely_news.iterrows():
                text = f"{row.get('title', '')} {row.get('summary', '')}".strip()
                if text:
                    scores.append(analyzer.analyze(text))
            avg_sentiment = sum(scores) / len(scores) if scores else 0.0

        # VADER compound score 范围 [-1, 1]，转换为 [0, 1] 置信度
        confidence = (avg_sentiment + 1.0) / 2.0
        direction = "bullish" if avg_sentiment > 0 else ("bearish" if avg_sentiment < 0 else "neutral")

        stock_scores.append({
            "symbol": symbol,
            "direction": direction,
            "confidence": confidence,
            "avg_sentiment": avg_sentiment,
        })
        print(f"  [VADER] {symbol}: sentiment={avg_sentiment:+.3f}, "
              f"confidence={confidence:.2f}, direction={direction}")

    # 筛选达标：bullish + confidence >= 阈值
    qualified = [
        s for s in stock_scores
        if s["direction"] == "bullish" and s["confidence"] >= config.CONFIDENCE_THRESHOLD
    ]

    if not qualified:
        print(f"  [VADER决策] {date}: 无达标股票，空仓")
        return {"holdings": [], "exposure": 0.0}

    # 按 confidence 降序取 Top-K
    qualified.sort(key=lambda x: x["confidence"], reverse=True)
    top_k_stocks = qualified[:config.TOP_K]

    # 仓位：波动率目标 + 逆波动率加权
    holdings, exposure, weights, vols = _position_sizing(
        top_k_stocks, market_state
    )

    print(f"  [VADER决策] {date}: "
          f"持仓={[h['symbol'] for h in holdings]}, "
          f"权重={[f'{w:.0%}' for w in weights]}, "
          f"日波动率={[f'{v:.2%}' for v in vols]}, "
          f"暴露={exposure:.0%}")

    return {
        "holdings": holdings,
        "exposure": exposure,
    }


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("测试决策函数...")

    # 模拟市场状态
    market_state = {
        "AAPL": {"current_price": 150.0, "rsi": 45.0, "macd_hist": 0.5, "atr": 2.0},
        "NVDA": {"current_price": 800.0, "rsi": 55.0, "macd_hist": -0.3, "atr": 20.0},
    }

    # 测试（无新闻数据 → 空仓）
    result = decide_formula_llm(
        date="2023-06-15",
        market_state=market_state,
        news_df=None,
    )
    print(f"\n决策结果：{result}")
