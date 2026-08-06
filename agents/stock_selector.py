"""
选股模块：从候选池中选出 Top-K 股票（等权持有）

职责：
  1. 接收候选股票池
  2. 对每只股票运行 LLM 分析（按 symbol 过滤新闻）
  3. 按置信度排序，选出 Top-K（bullish + confidence 最高）
  4. 持有优先：仍在 Top-K 的持仓不动，只替换掉出榜的
  5. 返回：选中的 K 只股票 + 各自置信度

Plan.md 阶段 3 对应：
  - 选股(每周)：LLM 分析候选池 → 按置信度排序返回 Top-K(默认 K=3)
  - 不足 K 只达标(confidence >= 阈值)则减少持仓数，全不达标则空仓
  - 持有优先：仍在 Top-K 的持仓不动，只替换掉出榜的
"""

from typing import List, Dict, Optional, Set
import pandas as pd

from agents.llm_analyst import LLMAnalystAgent
from core.data.news_data import get_news_at_date
from core import config


# 默认候选池
DEFAULT_CANDIDATE_POOL = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN"]


def analyze_candidate_pool(
    date: str,
    candidate_pool: List[str],
    news_df: pd.DataFrame,
    provider: str = None,
    model: str = None,
) -> List[Dict]:
    """
    对候选池中每只股票运行 LLM 分析。

    参数：
      date: 当前日期（格式 "YYYY-MM-DD"）
      candidate_pool: 候选股票列表
      news_df: 新闻 DataFrame（包含 symbol 列，用于按股票过滤）
      provider: LLM 提供商（默认 config.DEFAULT_LLM_PROVIDER）
      model: 模型名称（默认 config.DEFAULT_LLM_MODEL）

    返回：
      分析结果列表，每项包含：
        {"symbol": "AAPL", "direction": "bullish", "confidence": 0.8, "analysis": {...}}
      按 confidence 降序排列。
    """
    if provider is None:
        provider = config.DEFAULT_LLM_PROVIDER
    if model is None:
        model = config.DEFAULT_LLM_MODEL

    agent = LLMAnalystAgent(provider=provider, model=model)
    results = []

    print(f"\n[选股分析] {date}: 分析 {len(candidate_pool)} 只候选股...")

    for symbol in candidate_pool:
        try:
            # 获取该股票的新闻（按 symbol 过滤）
            news_list = _get_news_for_symbol(news_df, symbol, date)

            # LLM 分析
            analysis = agent.analyze(symbol, date, news_list)

            direction = analysis.get("direction", "neutral")
            confidence = float(analysis.get("confidence", 0.0))

            print(f"  {symbol}: {direction} (confidence={confidence:.2f}, news={len(news_list)}条)")

            results.append({
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "analysis": analysis,
            })

        except Exception as e:
            print(f"  [WARN] {symbol} 分析失败：{e}")
            continue

    # 按 confidence 降序排列
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


def select_top_k(
    date: str,
    candidate_pool: List[str],
    news_df: pd.DataFrame,
    current_holdings: Set[str] = None,
    top_k: int = None,
    confidence_threshold: float = None,
    provider: str = None,
    model: str = None,
) -> Dict:
    """
    从候选池中选出 Top-K 股票（持有优先）。

    参数：
      date: 当前日期
      candidate_pool: 候选股票列表
      news_df: 新闻 DataFrame（包含 symbol 列）
      current_holdings: 当前持有的股票集合（用于持有优先逻辑）
      top_k: 选出的最大股票数（默认 config.TOP_K）
      confidence_threshold: 置信度阈值（默认 config.CONFIDENCE_THRESHOLD）
      provider: LLM 提供商
      model: 模型名称

    返回：
      {
        "holdings": [
          {"symbol": "AAPL", "confidence": 0.8, "direction": "bullish"},
          {"symbol": "NVDA", "confidence": 0.7, "direction": "bullish"},
        ],
        "avg_confidence": 0.75,
        "all_analysis": [...],  # 所有候选的分析结果
      }
      如果无股票达标，返回 {"holdings": [], "avg_confidence": 0.0, ...}
    """
    if top_k is None:
        top_k = config.TOP_K
    if confidence_threshold is None:
        confidence_threshold = config.CONFIDENCE_THRESHOLD
    if current_holdings is None:
        current_holdings = set()

    # 1. 分析所有候选
    all_results = analyze_candidate_pool(
        date=date,
        candidate_pool=candidate_pool,
        news_df=news_df,
        provider=provider,
        model=model,
    )

    # 2. 筛选达标股票：direction == "bullish" 且 confidence >= 阈值
    qualified = [
        r for r in all_results
        if r["direction"] == "bullish" and r["confidence"] >= confidence_threshold
    ]

    print(f"  [筛选] 达标股票：{len(qualified)} 只 "
          f"({', '.join(r['symbol'] for r in qualified) if qualified else '无'})")

    if not qualified:
        print(f"  [结果] 无股票达到阈值 {confidence_threshold}，选择空仓")
        return {
            "holdings": [],
            "avg_confidence": 0.0,
            "all_analysis": all_results,
        }

    # 3. 取 Top-K（已按 confidence 降序排列）
    top_k_stocks = qualified[:top_k]
    top_k_symbols = {r["symbol"] for r in top_k_stocks}

    # 4. 持有优先逻辑
    # 如果当前持仓中有股票仍在 Top-K 中，保留它们
    # 只替换掉出榜的（从 Top-K 中消失的）
    if current_holdings:
        # 当前持仓中仍在 Top-K 的 → 保留
        retained = current_holdings & top_k_symbols
        # 当前持仓中出榜的 → 需要替换
        dropped = current_holdings - top_k_symbols
        # Top-K 中不在当前持仓的 → 新股
        new_entries = top_k_symbols - current_holdings

        if retained:
            print(f"  [持有优先] 保留：{retained}")
        if dropped:
            print(f"  [持有优先] 出榜：{dropped}")
        if new_entries:
            print(f"  [持有优先] 新进：{new_entries}")

        # 最终持仓 = 保留的 + 新股（按 confidence 排序取 Top-K）
        # 保留的优先占位，剩余名额给新股
        final_symbols = list(retained)
        remaining_slots = top_k - len(final_symbols)
        for r in top_k_stocks:
            if r["symbol"] not in retained and remaining_slots > 0:
                final_symbols.append(r["symbol"])
                remaining_slots -= 1

        # 构建最终持仓列表（保持 confidence 排序）
        final_holdings = []
        for r in top_k_stocks:
            if r["symbol"] in final_symbols:
                final_holdings.append({
                    "symbol": r["symbol"],
                    "confidence": r["confidence"],
                    "direction": r["direction"],
                })
        # 也加入保留的（可能在 qualified 之外但仍在持仓中）
        # 这种情况不会发生，因为 retained ⊆ top_k_symbols ⊆ qualified
    else:
        # 首次选股，无持有优先逻辑
        final_holdings = [
            {
                "symbol": r["symbol"],
                "confidence": r["confidence"],
                "direction": r["direction"],
            }
            for r in top_k_stocks
        ]

    # 5. 计算平均置信度
    avg_confidence = (
        sum(h["confidence"] for h in final_holdings) / len(final_holdings)
        if final_holdings else 0.0
    )

    print(f"  [结果] 最终持仓 {len(final_holdings)} 只："
          f"{', '.join(h['symbol'] + f'({h['confidence']:.2f})' for h in final_holdings)}")
    print(f"  [结果] 平均置信度：{avg_confidence:.2f}")

    return {
        "holdings": final_holdings,
        "avg_confidence": avg_confidence,
        "all_analysis": all_results,
    }


def _get_news_for_symbol(
    news_df: pd.DataFrame,
    symbol: str,
    date: str,
) -> List[Dict]:
    """
    获取指定股票在指定日期的新闻。

    如果 news_df 有 "symbol" 列，按 symbol 过滤。
    否则返回所有新闻（兼容旧数据）。
    """
    if news_df is None or len(news_df) == 0:
        return []

    # 按 symbol 过滤（如果有 symbol 列）
    if "symbol" in news_df.columns:
        filtered_df = news_df[news_df["symbol"] == symbol]
    else:
        filtered_df = news_df

    return get_news_at_date(filtered_df, symbol, date)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("测试选股模块（Top-K + 持有优先）...")
    print("=" * 60)

    # 模拟新闻数据（带 symbol 列）
    test_news = pd.DataFrame([
        {
            "datetime": pd.Timestamp("2024-01-15 10:00:00"),
            "title": "Apple reports strong earnings",
            "summary": "Apple beat expectations with Q4 revenue of $120B",
            "source": "Finnhub",
            "sentiment_score": 0.8,
            "symbol": "AAPL",
        },
        {
            "datetime": pd.Timestamp("2024-01-15 11:00:00"),
            "title": "NVIDIA announces new AI chip",
            "summary": "NVIDIA's new H200 chip shows 2x performance improvement",
            "source": "Finnhub",
            "sentiment_score": 0.9,
            "symbol": "NVDA",
        },
    ])

    # 测试 Top-K 选股
    result = select_top_k(
        date="2024-01-15",
        candidate_pool=["AAPL", "NVDA"],
        news_df=test_news,
        current_holdings=set(),
        top_k=2,
        confidence_threshold=0.5,
    )

    print(f"\n选股结果：")
    print(f"  持仓：{result['holdings']}")
    print(f"  平均置信度：{result['avg_confidence']:.2f}")
