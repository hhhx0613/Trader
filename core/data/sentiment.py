"""
情绪分析模块（SentimentAnalyzer）

职责：
  1. 对新闻标题/摘要进行情绪打分（-1.0 ~ 1.0）
  2. 支持多种方法：规则版（VADER）/ LLM 版（未来扩展）

设计说明：
  - 阶段 1-2 先用规则版（VADER），快速验证流程
  - 阶段 3+ 可以切换到 LLM 版（更准确，但贵）
  - 两种方法接口一致，可以随时切换

为什么不用 LLM 做情绪分析？
  - 数值推理弱、不可复现、贵
  - VADER 已经足够好（金融文本专用词典）
  - LLM 更适合做"选股决策"，而不是"情绪打分"
"""

from typing import List, Dict
import pandas as pd

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    VADER_AVAILABLE = True
except ImportError:
    VADER_AVAILABLE = False
    print("[SentimentAnalyzer] [WARN] vaderSentiment 未安装，将使用简单规则")


class SentimentAnalyzer:
    """
    情绪分析器，对文本进行情绪打分。
    
    使用方法：
      analyzer = SentimentAnalyzer()
      score = analyzer.analyze("Apple reports strong earnings")
      # 返回 0.75（看涨）
    """
    
    def __init__(self, method: str = "vader"):
        """
        参数：
          method: 情绪分析方法
            - "vader": 使用 VADER（默认，推荐）
            - "simple": 简单规则（备用）
        """
        self.method = method
        
        if method == "vader":
            if not VADER_AVAILABLE:
                print("[SentimentAnalyzer] VADER 不可用，切换到 simple 方法")
                self.method = "simple"
            else:
                self.analyzer = SentimentIntensityAnalyzer()
    
    def analyze(self, text: str) -> float:
        """
        对单条文本进行情绪分析。
        
        参数：
          text: 待分析文本（标题或摘要）
        
        返回：
          float，情绪分数（-1.0 ~ 1.0）
            - 1.0: 极度看涨
            - 0.0: 中性
            - -1.0: 极度看跌
        """
        if not text or not isinstance(text, str):
            return 0.0
        
        if self.method == "vader":
            return self._analyze_vader(text)
        else:
            return self._analyze_simple(text)
    
    def analyze_batch(self, texts: List[str]) -> List[float]:
        """
        批量分析多条文本的情绪。
        
        参数：
          texts: 文本列表
        
        返回：
          List[float]，每条文本的情绪分数
        """
        return [self.analyze(text) for text in texts]
    
    def _analyze_vader(self, text: str) -> float:
        """
        使用 VADER 进行情绪分析。
        
        VADER 特点：
          - 专为社交媒体和金融文本优化
          - 考虑否定词、程度副词、标点等
          - 返回 compound score（-1.0 ~ 1.0）
        """
        scores = self.analyzer.polarity_scores(text)
        return scores["compound"]
    
    def _analyze_simple(self, text: str) -> float:
        """
        简单规则版情绪分析（备用）。
        
        基于关键词匹配：
          - 看涨词：+0.5
          - 看跌词：-0.5
          - 中性词：0.0
        """
        text_lower = text.lower()
        
        # 看涨关键词
        bullish_words = [
            "up", "rise", "gain", "grow", "strong", "bullish",
            "profit", "beat", "surge", "rally", "optimistic",
            "涨", "上升", "增长", "强劲", "看涨", "利润", "突破"
        ]
        
        # 看跌关键词
        bearish_words = [
            "down", "fall", "drop", "decline", "weak", "bearish",
            "loss", "miss", "crash", "plunge", "pessimistic",
            "跌", "下降", "下跌", "疲软", "看跌", "亏损", "崩盘"
        ]
        
        score = 0.0
        for word in bullish_words:
            if word in text_lower:
                score += 0.3
        
        for word in bearish_words:
            if word in text_lower:
                score -= 0.3
        
        # 归一化到 [-1, 1]
        return max(-1.0, min(1.0, score))


# ==================== 便捷函数 ====================

def analyze_news_sentiment(
    news_df: pd.DataFrame,
    method: str = "vader",
) -> pd.DataFrame:
    """
    对新闻 DataFrame 进行情绪分析，更新 sentiment_score 列。
    
    参数：
      news_df: 新闻 DataFrame（必须有 title 和 summary 列）
      method: 情绪分析方法（"vader" 或 "simple"）
    
    返回：
      新的 DataFrame（不修改原始输入），含更新后的 sentiment_score 列
    """
    result = news_df.copy()
    analyzer = SentimentAnalyzer(method=method)
    
    # 对每条新闻的标题 + 摘要进行分析
    scores = []
    for _, row in result.iterrows():
        text = f"{row['title']} {row['summary']}".strip()
        score = analyzer.analyze(text)
        scores.append(score)
    
    result["sentiment_score"] = scores
    
    return result


# ==================== 测试入口 ====================

if __name__ == "__main__":
    # 测试情绪分析
    print("测试情绪分析...")
    analyzer = SentimentAnalyzer(method="vader")
    
    test_texts = [
        "Apple reports strong earnings, stock surges 5%",
        "Tesla misses expectations, shares fall 10%",
        "Microsoft announces new product launch",
        "Market crashes amid economic uncertainty",
    ]
    
    for text in test_texts:
        score = analyzer.analyze(text)
        print(f"  {score:+.2f} | {text}")
    
    # 测试批量分析
    print("\n测试批量分析...")
    scores = analyzer.analyze_batch(test_texts)
    print(f"  平均情绪分数：{sum(scores) / len(scores):+.2f}")
