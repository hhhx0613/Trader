"""
决策函数：把 LLM 分析结果转换为交易决策

Plan.md 阶段 3 决策逻辑：
  1. 选股(每周)：LLM 分析候选池 → Top-K 等权
  2. 仓位：总仓位暴露 = 平均 confidence × 风控允许上限（纯公式）
  3. 风控：risk_manager 硬规则（止损、回撤、连亏降仓）
  4. 执行：回测引擎按调仓周期执行

返回格式：
  {
    "holdings": [{"symbol": "AAPL", "weight": 0.5}, ...],  # 持仓列表 + 等权权重
    "exposure": 0.6,  # 总仓位暴露（0~1）
  }
"""

import pandas as pd
from typing import Dict, List, Set
from agents.stock_selector import select_top_k, DEFAULT_CANDIDATE_POOL
from core import config
from core.data.sentiment import SentimentAnalyzer


def decide_formula_llm(
    date,
    market_state: Dict,
    candidate_pool: List[str] = None,
    news_df=None,
    current_holdings: Set[str] = None,
) -> Dict:
    """
    公式版决策函数：LLM Top-K 选股 + 技术指标确认 + 公式仓位

    参数：
      date: 当前日期
      market_state: 市场状态（由 multi_stock_engine 提供，包含各股技术指标）
      candidate_pool: 候选股票池
      news_df: 新闻 DataFrame（包含 symbol 列）
      current_holdings: 当前持有的股票集合（用于持有优先）

    返回：
      {
        "holdings": [
          {"symbol": "AAPL", "weight": 0.5},
          {"symbol": "NVDA", "weight": 0.5},
        ],
        "exposure": 0.6,  # 总仓位暴露
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
        )
    else:
        # 没有新闻数据时，不交易
        print(f"  [决策] {date}: 无新闻数据，选择空仓")
        return {"holdings": [], "exposure": 0.0}

    holdings_result = selection["holdings"]

    if not holdings_result:
        return {"holdings": [], "exposure": 0.0}

    # ========== 2. 技术指标确认（对每只选中的股票）==========
    adjusted_confidences = []
    for h in holdings_result:
        symbol = h["symbol"]
        confidence = h["confidence"]

        # 从 market_state 获取该股票的技术指标
        stock_state = market_state.get(symbol, {})
        rsi = stock_state.get("rsi", 50.0)
        macd_hist = stock_state.get("macd_hist", 0.0)

        # RSI 调整
        if rsi > 70:  # 超买
            confidence *= 0.7
            print(f"  [调整] {symbol} RSI={rsi:.1f} 超买，置信度降至 {confidence:.2f}")
        elif rsi < 30:  # 超卖
            confidence *= 1.2
            confidence = min(confidence, 1.0)
            print(f"  [调整] {symbol} RSI={rsi:.1f} 超卖，置信度提升至 {confidence:.2f}")

        # MACD 确认
        if macd_hist > 0:
            confidence *= 1.1
            confidence = min(confidence, 1.0)
        elif macd_hist < 0:
            confidence *= 0.9

        adjusted_confidences.append(confidence)

    # ========== 3. 计算仓位 ==========
    # 等权：每只股票权重 = 1/K
    k = len(holdings_result)
    weight_per_stock = 1.0 / k

    holdings = [
        {"symbol": h["symbol"], "weight": weight_per_stock}
        for h in holdings_result
    ]

    # 总仓位暴露 = 平均 confidence × 风控允许上限
    avg_confidence = sum(adjusted_confidences) / len(adjusted_confidences)
    exposure = avg_confidence * config.MAX_EXPOSURE_RATIO

    # 风控截断：不超过 MAX_POSITION_RATIO
    exposure = min(exposure, config.MAX_POSITION_RATIO)

    print(f"  [决策] {date}: "
          f"持仓={[h['symbol'] for h in holdings]}, "
          f"等权={weight_per_stock:.0%}, "
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
) -> Dict:
    """
    VADER 规则版决策函数（对照基线）

    与 decide_formula_llm 接口完全一致，但用 VADER 情绪分代替 LLM 分析。
    用于论文消融实验：“LLM vs 规则情绪”对比。

    逻辑：
      1. 对每只候选股，取时点对齐的新闻，用 VADER 打分
      2. 平均情绪分 > 0 且 >= 阈值 → bullish，confidence = 平均情绪分
      3. 按 confidence 排序取 Top-K，等权 + 公式仓位

    参数和返回格式同 decide_formula_llm。
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

    k = len(top_k_stocks)
    weight_per_stock = 1.0 / k

    holdings = [
        {"symbol": s["symbol"], "weight": weight_per_stock}
        for s in top_k_stocks
    ]

    avg_confidence = sum(s["confidence"] for s in top_k_stocks) / k
    exposure = avg_confidence * config.MAX_EXPOSURE_RATIO
    exposure = min(exposure, config.MAX_POSITION_RATIO)

    print(f"  [VADER决策] {date}: 持仓={[h['symbol'] for h in holdings]}, "
          f"等权={weight_per_stock:.0%}, 暴露={exposure:.0%}")

    return {
        "holdings": holdings,
        "exposure": exposure,
    }


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("测试决策函数...")

    # 模拟市场状态
    market_state = {
        "AAPL": {"current_price": 150.0, "rsi": 45.0, "macd_hist": 0.5},
        "NVDA": {"current_price": 800.0, "rsi": 55.0, "macd_hist": -0.3},
    }

    # 测试（无新闻数据 → 空仓）
    result = decide_formula_llm(
        date="2023-06-15",
        market_state=market_state,
        news_df=None,
    )
    print(f"\n决策结果：{result}")
