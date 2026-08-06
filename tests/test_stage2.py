"""
阶段 2 测试脚本：LLM Analyst Agent

测试内容：
  1. LLM Agent 分析新闻 (GLM)
  2. Top-K 选股（stock_selector）
  3. 决策函数转换（decide_formula_llm）
"""

from pathlib import Path

from agents.llm_analyst import LLMAnalystAgent
from agents.stock_selector import select_top_k
from agents.decision_func import decide_formula_llm
import pandas as pd


def test_llm_agent():
    """测试 LLM Agent 功能。"""
    print("=" * 60)
    print("  阶段 2 测试：LLM Analyst Agent")
    print("=" * 60)

    # ========== 测试 1: 真实 LLM 分析 (GLM) ==========
    print("\n[测试 1] 真实 LLM 分析 (GLM)...")
    agent = LLMAnalystAgent(provider="glm", model="glm-4")

    test_news = [
        {
            "datetime": "2023-06-15 09:00:00",
            "title": "Apple reports strong earnings",
            "summary": "Apple beat expectations with Q2 revenue of $90B",
            "source": "Finnhub",
            "sentiment_score": 0.8,
        },
        {
            "datetime": "2023-06-15 10:00:00",
            "title": "Apple announces new product",
            "summary": "Apple unveils new AI features",
            "source": "Finnhub",
            "sentiment_score": 0.6,
        }
    ]

    result = agent.analyze("AAPL", "2023-06-15", test_news)
    print(f"  [OK] 分析结果:")
    print(f"    direction: {result['direction']}")
    print(f"    confidence: {result['confidence']}")
    print(f"    reasons: {result['reasons']}")

    # ========== 测试 2: Top-K 选股 ==========
    print("\n[测试 2] Top-K 选股...")

    # 构造带 symbol 列的新闻 DataFrame
    test_news_df = pd.DataFrame([
        {
            "datetime": pd.Timestamp("2023-06-15 09:00:00"),
            "title": "Apple reports strong earnings",
            "summary": "Apple beat expectations with Q2 revenue of $90B",
            "source": "Finnhub",
            "sentiment_score": 0.8,
            "symbol": "AAPL",
        },
        {
            "datetime": pd.Timestamp("2023-06-15 10:00:00"),
            "title": "NVIDIA announces new AI chip",
            "summary": "NVIDIA's new H200 chip shows 2x performance",
            "source": "Finnhub",
            "sentiment_score": 0.9,
            "symbol": "NVDA",
        },
    ])

    selection = select_top_k(
        date="2023-06-15",
        candidate_pool=["AAPL", "NVDA"],
        news_df=test_news_df,
        current_holdings=set(),
        top_k=2,
        confidence_threshold=0.5,
    )
    print(f"  [OK] 选股结果:")
    print(f"    持仓: {[h['symbol'] for h in selection['holdings']]}")
    print(f"    平均置信度: {selection['avg_confidence']:.2f}")

    # ========== 测试 3: 决策函数（完整接口）==========
    print("\n[测试 3] 决策函数转换...")

    market_state = {
        "AAPL": {"current_price": 150.0, "rsi": 45.0, "macd_hist": 0.5},
        "NVDA": {"current_price": 800.0, "rsi": 55.0, "macd_hist": -0.3},
    }

    decision = decide_formula_llm(
        date="2023-06-15",
        market_state=market_state,
        candidate_pool=["AAPL", "NVDA"],
        news_df=test_news_df,
        current_holdings=set(),
    )

    print(f"  [OK] 决策结果:")
    print(f"    holdings: {decision['holdings']}")
    print(f"    exposure: {decision['exposure']:.1%}")

    # ========== 测试 4: 无新闻数据 → 空仓 ==========
    print("\n[测试 4] 无新闻数据 → 空仓...")
    decision_empty = decide_formula_llm(
        date="2023-06-15",
        market_state=market_state,
        news_df=None,
    )
    print(f"  [OK] 空仓决策: holdings={decision_empty['holdings']}, exposure={decision_empty['exposure']}")

    # ========== 总结 ==========
    print("\n" + "=" * 60)
    print("  [DONE] 阶段 2 测试完成！")
    print("=" * 60)
    print("\n下一步：")
    print("  1. 确保 GLM_API_KEY 环境变量已配置")
    print("  2. 开始阶段 3：完整决策链回测")
    print("=" * 60)


if __name__ == "__main__":
    test_llm_agent()
