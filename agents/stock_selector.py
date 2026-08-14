"""
选股模块：从候选池中选出 Top-K 股票

职责：
  1. 接收候选股票池
  2. 对每只股票运行 LLM 分析（按 symbol 过滤新闻）
  3. 按 composite_score × confidence 排序，选出 Top-K
     - composite_score（代码从 per_news 确定性计算）：信号方向+强度
     - confidence（LLM 评估的证据质量）：信号可信度
     - 乘积 = 期望信号强度，同时要求方向明确且证据可靠
  4. 持有优先：仍在 Top-K 的持仓不动，只替换掉出榜的
  5. 返回：选中的 K 只股票 + 各自 composite_score 和 confidence

Plan.md 阶段 3 对应：
  - 选股(每周)：LLM 分析候选池 → 按 composite_score × confidence 排序返回 Top-K(默认 K=5)
  - 不足 K 只达标则减少持仓数，全不达标则空仓
  - 持有优先：仍在 Top-K 的持仓不动，只替换掉出榜的
"""

from typing import List, Dict, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

from agents.llm_analyst import LLMAnalystAgent
from agents.confidence_calibrator import get_calibrator
from core.data.news_data import get_news_at_date
from core import config

# 全局开关：禁用 L1 记忆（消融实验时临时设为 True，需同步清空 LLM 缓存）
_DISABLE_L1_MEMORY = False

# 校准数据收集器：回测过程中收集 LLM 决策，回测结束后用于训练校准表
_collected_decisions: List[Dict] = []


def get_collected_decisions() -> List[Dict]:
    """获取回测过程中收集的 LLM 决策记录"""
    return _collected_decisions


def clear_collected_decisions() -> None:
    """清空收集的决策记录（新一轮回测前调用）"""
    _collected_decisions.clear()


# 默认候选池（10 只股票，跨行业分散化）
DEFAULT_CANDIDATE_POOL = [
    # 科技（5 只）
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
    # 金融（2 只）
    "JPM", "V",
    # 医疗（2 只）
    "JNJ", "UNH",
    # 消费（1 只）
    "WMT"
]


def _analyze_single_stock(
    symbol: str,
    date: str,
    news_df: pd.DataFrame,
    provider: str,
    model: str,
    calibrator,
    market_data: Optional[Dict] = None,
) -> Dict:
    """
    分析单只股票（供线程池调用）。

    每只股票创建独立的 LLMAnalystAgent 实例，避免线程间共享 HTTP 连接。

    参数：
      market_data: 可选，行情数据 {symbol: DataFrame}，用于 L1 记忆注入
                   「上次判断后的实际市场反应」。为 None 时降级为旧的一致性记忆。
    """
    try:
        news_list = _get_news_for_symbol(news_df, symbol, date)

        if not news_list:
            return {
                "symbol": symbol,
                "direction": "neutral",
                "composite_score": 0.0,
                "confidence": 0.0,
                "raw_confidence": 0.0,
                "analysis": {"direction": "neutral", "confidence": 0.0, "composite_score": 0.0},
            }

        # 提取该股票的收盘价序列（Close 列），供 L1 记忆计算实际收益
        price_series = None
        if market_data is not None and symbol in market_data:
            df = market_data[symbol]
            if isinstance(df, pd.DataFrame) and "close" in df.columns:
                price_series = df["close"]

        # 每线程创建独立 agent（隔离 HTTP session + SQLite 连接）
        agent = LLMAnalystAgent(
            provider=provider, model=model,
            enable_l1_memory=not _DISABLE_L1_MEMORY,
        )
        analysis = agent.analyze(symbol, date, news_list, price_series=price_series)

        direction = analysis.get("direction", "neutral")
        composite_score = float(analysis.get("composite_score", 0.0))
        raw_confidence = float(analysis.get("confidence", 0.0))

        if calibrator and direction != "neutral":
            confidence = calibrator.calibrate(raw_confidence)
            if abs(confidence - raw_confidence) > 0.01:
                print(f"  [校准] {symbol}: {raw_confidence:.2f} → {confidence:.2f}")
        else:
            confidence = raw_confidence

        ranking_score = composite_score * confidence
        print(f"  {symbol}: {direction} (score={composite_score:+.3f}, conf={confidence:.2f}, rank={ranking_score:.3f}, news={len(news_list)}条)")

        # 收集决策记录（用于回测后训练校准表）
        _collected_decisions.append({
            "symbol": symbol,
            "date": str(date),
            "direction": direction,
            "confidence": raw_confidence,  # 记录原始 confidence，校准前
        })

        return {
            "symbol": symbol,
            "direction": direction,
            "composite_score": composite_score,
            "confidence": confidence,
            "raw_confidence": raw_confidence,
            "ranking_score": ranking_score,
            "analysis": analysis,
        }

    except Exception as e:
        print(f"  [WARN] {symbol} 分析失败：{e}")
        return {
            "symbol": symbol,
            "direction": "neutral",
            "composite_score": 0.0,
            "confidence": 0.0,
            "raw_confidence": 0.0,
            "ranking_score": 0.0,
            "analysis": {"direction": "neutral", "confidence": 0.0, "composite_score": 0.0},
        }


def analyze_candidate_pool(
    date: str,
    candidate_pool: List[str],
    news_df: pd.DataFrame,
    provider: str = None,
    model: str = None,
    enable_calibration: bool = False,  
    max_workers: int = None,
    market_data: Optional[Dict] = None,
) -> List[Dict]:
    """
    对候选池中每只股票并行运行 LLM 分析。

    参数：
      date: 当前日期（格式 "YYYY-MM-DD"）
      candidate_pool: 候选股票列表
      news_df: 新闻 DataFrame（包含 symbol 列，用于按股票过滤）
      provider: LLM 提供商（默认 config.DEFAULT_LLM_PROVIDER）
      model: 模型名称（默认 config.DEFAULT_LLM_MODEL）
      enable_calibration: 是否启用置信度校准（默认 False，已禁用 - 原因见 calibration_removal_report.md）
      max_workers: 最大并行线程数（默认 min(候选数，10)）
      market_data: 可选，行情数据 {symbol: DataFrame}，用于 L1 记忆注入实际收益反馈

    返回：
      分析结果列表，每项包含：
        {"symbol": "AAPL", "direction": "bullish", "composite_score": 0.6, "confidence": 0.8, "ranking_score": 0.48, ...}
      按 ranking_score（= composite_score × confidence）降序排列。
    """
    if provider is None:
        provider = config.DEFAULT_LLM_PROVIDER
    if model is None:
        model = config.DEFAULT_LLM_MODEL

    calibrator = get_calibrator() if enable_calibration else None

    print(f"\n[选股分析] {date}: 并行分析 {len(candidate_pool)} 只候选股...")

    # 并行调用 LLM（I/O 密集型，线程池即可）
    if max_workers is None:
        max_workers = min(len(candidate_pool), 10)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _analyze_single_stock,
                symbol, date, news_df, provider, model, calibrator, market_data,
            ): symbol
            for symbol in candidate_pool
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    # 按 ranking_score（= composite_score × confidence）降序排列
    results.sort(key=lambda x: x.get("ranking_score", 0.0), reverse=True)
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
    market_data: Optional[Dict] = None,
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
      market_data: 可选，行情数据 {symbol: DataFrame}，用于 L1 记忆注入实际收益反馈

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
        market_data=market_data,
    )

    # 2. 筛选达标股票：composite_score > 0.1（bullish 门槛）且 confidence >= 阈值
    qualified = [
        r for r in all_results
        if r.get("composite_score", 0.0) > 0.1 and r["confidence"] >= confidence_threshold
    ]

    print(f"  [筛选] 达标股票：{len(qualified)} 只 "
          f"({', '.join(r['symbol'] for r in qualified) if qualified else '无'})")

    if not qualified:
        print(f"  [结果] 无股票达到阈值 {confidence_threshold}，选择空仓")
        return {
            "holdings": [],
            "avg_composite_score": 0.0,
            "avg_confidence": 0.0,
            "all_analysis": all_results,
        }

    # 3. 取 Top-K（已按 ranking_score = composite_score × confidence 降序排列）
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

        # 最终持仓 = 保留的 + 新股（按 ranking_score 排序取 Top-K）
        # 保留的优先占位，剩余名额给新股
        final_symbols = list(retained)
        remaining_slots = top_k - len(final_symbols)
        for r in top_k_stocks:
            if r["symbol"] not in retained and remaining_slots > 0:
                final_symbols.append(r["symbol"])
                remaining_slots -= 1

        # 构建最终持仓列表（保持 ranking_score 排序）
        final_holdings = []
        for r in top_k_stocks:
            if r["symbol"] in final_symbols:
                final_holdings.append({
                    "symbol": r["symbol"],
                    "composite_score": r.get("composite_score", 0.0),
                    "confidence": r["confidence"],
                    "ranking_score": r.get("ranking_score", 0.0),
                    "direction": r["direction"],
                })
        # 也加入保留的（可能在 qualified 之外但仍在持仓中）
        # 这种情况不会发生，因为 retained ⊆ top_k_symbols ⊆ qualified
    else:
        # 首次选股，无持有优先逻辑
        final_holdings = [
            {
                "symbol": r["symbol"],
                "composite_score": r.get("composite_score", 0.0),
                "confidence": r["confidence"],
                "ranking_score": r.get("ranking_score", 0.0),
                "direction": r["direction"],
            }
            for r in top_k_stocks
        ]

    # 5. 统计汇总
    avg_confidence = (
        sum(h["confidence"] for h in final_holdings) / len(final_holdings)
        if final_holdings else 0.0
    )
    avg_score = (
        sum(h["composite_score"] for h in final_holdings) / len(final_holdings)
        if final_holdings else 0.0
    )

    holdings_str = ", ".join(
        f"{h['symbol']}(score={h['composite_score']:+.2f},conf={h['confidence']:.2f})"
        for h in final_holdings
    )
    print(f"  [结果] 最终持仓 {len(final_holdings)} 只：{holdings_str}")
    print(f"  [结果] 平均 composite_score：{avg_score:+.3f}，平均 confidence：{avg_confidence:.2f}")

    return {
        "holdings": final_holdings,
        "avg_composite_score": avg_score,
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

    return get_news_at_date(news_df, symbol, date)


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
