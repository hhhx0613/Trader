"""
LLM Analyst Agent（新闻分析师）

职责：
  1. 读取新闻数据（从阶段 1 的管道）
  2. 用 LLM 分析新闻，生成结构化观点
  3. 输出：direction, confidence, reasons, sources

设计说明：
  - 阶段 2 先做单 agent（news-analyst）
  - 阶段 4 扩展为多 agent 协同（用 LangGraph）
  - 结构化输出：强制 JSON schema，保证可解析

为什么不用 LangGraph（阶段 2）：
  - 单 agent 不需要复杂编排
  - 直接调 OpenAI API 更快更简单
  - 阶段 4 再引入 LangGraph 做多 agent 协同
"""

import json
import hashlib
import pandas as pd
from typing import Dict, List, Optional
from pathlib import Path

from core.data.news_data import get_news_at_date
from utils.llm_client import LLMClient

# LLM 输出缓存目录
_LLM_CACHE_DIR = Path(__file__).parent.parent / "core" / "data" / "cache" / "llm"
_LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class LLMAnalystAgent:
    """
    LLM 新闻分析师 Agent
    
    使用方法：
      agent = LLMAnalystAgent()
      analysis = agent.analyze("AAPL", "2023-06-15", news_list)
      # 返回：{"direction": "bullish", "confidence": 0.8, ...}
    """
    
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        """
        参数：
          provider: LLM 提供商 ("openai", "glm", "deepseek")
            如果为 None，则从环境变量 DEFAULT_LLM_PROVIDER 读取
          model: 模型名称
            如果为 None，则使用该提供商的默认模型
        """
        self.provider = provider
        self.model = model
        self.prompt_version = "v1.0"  # 记录 prompt 版本（可复现性）
        
        # 初始化 LLM 客户端
        try:
            self.llm_client = LLMClient(provider=provider, model=model)
            # 更新为实际使用的模型
            self.model = self.llm_client.model
        except Exception as e:
            raise RuntimeError(f"LLM 客户端初始化失败：{e}")
    
    def analyze(
        self,
        symbol: str,
        date: str,
        news_list: List[Dict],
    ) -> Dict:
        """
        分析新闻，生成结构化观点。
        
        参数：
          symbol: 股票代码（如 "AAPL"）
          date: 分析日期（格式 "YYYY-MM-DD"）
          news_list: 新闻列表（由 get_news_at_date 返回）
        
        返回：
          {
            "symbol": "AAPL",
            "direction": "bullish | neutral | bearish",
            "confidence": 0.0-1.0,
            "reasons": ["...", "..."],
            "sources": ["新闻标题/链接"],
            "analysis_date": "2023-06-15"
          }
        """
        # 调用 LLM API
        try:
            return self._analyze_with_llm(symbol, date, news_list)
        except Exception as e:
            print(f"[LLMAnalystAgent] LLM 调用失败：{e}")
            raise
    
    def _analyze_with_llm(self, symbol: str, date: str, news_list: List[Dict]) -> Dict:
        """
        使用 LLM 客户端分析新闻（带缓存）。
        """
        # 构建 prompt
        prompt = self._build_prompt(symbol, date, news_list)
        
        # 检查缓存（可复现性：相同输入直接返回缓存结果）
        cache_key = self._compute_cache_key(symbol, date, prompt)
        cached = self._load_cache(cache_key)
        if cached is not None:
            print(f"  [LLM Cache] 命中缓存: {symbol} @ {date}")
            return cached
        
        # 调用 LLM 客户端
        response = self.llm_client.chat_json(
            message=prompt,
            system_prompt="你是一个专业的股票分析师。",
            temperature=0.1,  # 低温保证可复现性
            max_tokens=1000,
        )
        
        # 检查是否有错误
        if "error" in response:
            raise RuntimeError(f"LLM 返回错误：{response['error']}")
        
        # 添加元数据
        response["symbol"] = symbol
        response["analysis_date"] = date
        response["prompt_version"] = self.prompt_version
        
        # 写入缓存
        self._save_cache(cache_key, response)
        
        return response
    
    def _build_prompt(self, symbol: str, date: str, news_list: List[Dict]) -> str:
        """
        构建 LLM prompt。
        """
        # 格式化新闻列表（处理可能的 NaN 值）
        news_lines = []
        for news in news_list[:5]:  # 最多取 5 条新闻
            dt = news.get('datetime', 'Unknown')
            title = news.get('title', 'No title')
            summary = news.get('summary', '')
            # 处理 summary 可能是 NaN 的情况
            if pd.isna(summary) or not isinstance(summary, str):
                summary = ''
            summary_text = summary[:100] if summary else 'No summary'
            news_lines.append(f"- {dt}: {title}\n  摘要：{summary_text}")
        
        news_text = "\n".join(news_lines)
        
        prompt = f"""
分析 {symbol} 在 {date} 的新闻，判断市场情绪和股价方向。

新闻列表：
{news_text}

请以 JSON 格式输出：
{{
  "symbol": "{symbol}",
  "direction": "bullish（看涨）| neutral（中性）| bearish（看跌）",
  "confidence": 0.0-1.0（置信度，基于新闻质量和数量）,
  "reasons": ["原因 1", "原因 2", ...],
  "sources": ["新闻标题 1", "新闻标题 2", ...]
}}

注意：
1. 只输出 JSON，不要其他文字
2. confidence 根据新闻数量和质量调整：
   - 5+ 条新闻：0.8-1.0
   - 2-4 条新闻：0.5-0.7
   - 0-1 条新闻：0.0-0.4
3. direction 必须准确，不要模棱两可
"""
        
        return prompt.strip()

    # ==================== LLM 输出缓存 ====================

    def _compute_cache_key(self, symbol: str, date: str, prompt: str) -> str:
        """
        计算缓存 key（基于 symbol + date + prompt 的 hash）。
        保证相同输入总是返回相同缓存。
        """
        content = f"{symbol}|{date}|{self.model}|{prompt}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _load_cache(self, cache_key: str) -> Optional[Dict]:
        """从本地文件加载 LLM 缓存。"""
        cache_file = _LLM_CACHE_DIR / f"{cache_key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def _save_cache(self, cache_key: str, data: Dict):
        """将 LLM 输出保存到本地缓存文件。"""
        cache_file = _LLM_CACHE_DIR / f"{cache_key}.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LLM Cache] 缓存写入失败（不影响使用）：{e}")


# ==================== 便捷函数 ====================

def analyze_stock_news(
    symbol: str,
    date: str,
    news_df,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict:
    """
    便捷函数：分析某只股票在某天的新闻。
    
    参数：
      symbol: 股票代码
      date: 分析日期
      news_df: 新闻 DataFrame（由 fetch_news 返回）
      provider: LLM 提供商 ("openai", "glm", "deepseek")
      model: 模型名称
    
    返回：
      LLM 分析结果字典
    """
    # 获取时点对齐的新闻
    news_list = get_news_at_date(news_df, symbol, date)
    
    # 创建 agent 并分析
    agent = LLMAnalystAgent(provider=provider, model=model)
    analysis = agent.analyze(symbol, date, news_list)
    
    return analysis


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("测试 LLM Analyst Agent...")
    
    # 测试 GLM
    agent = LLMAnalystAgent(provider="glm", model="glm-4")
    
    # 模拟新闻数据
    news_list = [
        {
            "datetime": "2023-06-15 09:00:00",
            "title": "Apple reports strong earnings",
            "summary": "Apple beat expectations with Q2 revenue of $90B",
            "source": "Finnhub",
            "sentiment_score": 0.8,
        }
    ]
    
    result = agent.analyze("AAPL", "2023-06-15", news_list)
    print(f"分析结果：{json.dumps(result, indent=2, ensure_ascii=False)}")
