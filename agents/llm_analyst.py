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
import sys
import hashlib
import pandas as pd
from typing import Dict, List, Optional
from pathlib import Path

# 支持直接运行本文件（python agents/llm_analyst.py）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.data.news_data import get_news_at_date
from core.data.llm_cache_db import get_cache_db
from utils.llm_client import LLMClient

# LLM 输出缓存目录
_LLM_CACHE_DIR = Path(__file__).parent.parent / "core" / "data" / "cache" / "llm"
_LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ==================== System Prompt（v2.0 结构化多维打分框架）====================
# 设计原则：
#   - 约束模型的思考维度（事件影响性、预期差、时效性、传导确定性）
#   - 不设定关键词到分数的硬映射（避免退化为 VADER 式词典匹配）
#   - 让 LLM 发挥语义理解优势，同时输出可解释、可追溯的结构化评分

_SYSTEM_PROMPT = """\
你是专业股票分析师。请对给定的股票新闻进行结构化多维分析，判断市场情绪和股价方向。

## 分析框架

对每条新闻，从以下 4 个维度独立打分：

### 1. 事件影响性（impact：-2 ~ +2）
对公司基本面（营收、利润、市场份额、技术壁垒）的实质影响程度。
- +2：重大利好（超预期财报、重大合同、突破性产品）
- +1：温和利好（小幅超预期、正面合作、分析师上调评级）
-  0：中性（例行公告、人事变动、行业一般动态）
- -1：温和利空（小幅不及预期、监管关注、竞争加剧）
- -2：重大利空（财报暴雷、核心产品失败、高管丑闻、重大诉讼）

### 2. 预期差（expectation_gap：-2 ~ +2）
与市场已有共识的偏离程度。
- +2：远超市场共识的意外利好
- +1：略超市场预期
-  0：符合预期，或新闻本身未涉及预期对比
- -1：略低于市场预期
- -2：远低于预期的意外利空

### 3. 时效性系数（timeliness：0.5 / 1.0 / 1.5）
影响持续时间的估计。
- 0.5：短期噪音（1-3 天内消化，如日内波动、未经证实的传闻、技术面反弹）
- 1.0：中期影响（1-4 周内逐步反映，如季度财报、产品发布）
- 1.5：长期结构性变化（1-3 个月以上持续影响，如战略转型、行业格局变化）

### 4. 传导确定性（certainty：0.5 / 1.0 / 1.5）
从事件到股价变动的逻辑链可靠性。
- 0.5：逻辑链弱或存在多重不确定性（传闻、间接影响、需多步传导）
- 1.0：逻辑链较清晰（直接影响、有历史参照）
- 1.5：因果关系明确且可验证（已公告的重大事件、历史同类事件均有明确反应）

### 单条新闻得分计算
score = (impact + expectation_gap) × timeliness × certainty
理论范围 [-6, +6]

### 综合得分（composite_score）
所有新闻 score 的加权平均（时效性高的权重更大），归一化到 [-1, 1]。
归一化方法：composite_score = tanh(加权平均 score / 3.0)

## 输出格式

严格按以下 JSON 结构输出，不要添加任何其他文字：

{
  "per_news": [
    {
      "index": 1,
      "impact": 1,
      "expectation_gap": 0,
      "timeliness": 1.0,
      "certainty": 1.0,
      "score": 1.0,
      "reasoning": "简要说明打分理由（50字以内）"
    }
  ],
  "composite_score": 0.75,
  "direction": "bullish",
  "confidence": 0.8,
  "key_factors": ["因素1", "因素2"],
  "reasons": ["综合原因1", "综合原因2"]
}

## 字段说明
- per_news：每条新闻的维度打分（与新闻列表序号对应）
- composite_score：综合得分，[-1, 1]，正=看涨，负=看跌
- direction：bullish（composite_score > 0.1）/ neutral（-0.1 ~ 0.1）/ bearish（< -0.1）
- confidence：置信度，硬性规则如下：
  - 5+ 条有实质内容的新闻：0.8-1.0
  - 2-4 条新闻：0.5-0.7
  - 0-1 条新闻：0.0-0.4
- key_factors：影响判断的核心因素（不超过 3 个）
- reasons：综合判断理由（不超过 3 条）

## 约束
1. 只输出 JSON，不要任何额外文字或 markdown 标记
2. 每条 news 的 reasoning 不超过 50 字
3. composite_score 必须在 [-1, 1] 范围内
4. 如果无新闻，per_news 为空数组，direction 为 neutral，confidence 为 0.0
"""


class LLMAnalystAgent:
    """
    LLM 新闻分析师 Agent
    
    使用方法：
      agent = LLMAnalystAgent()
      analysis = agent.analyze("AAPL", "2023-06-15", news_list)
      # 返回：{"direction": "bullish", "confidence": 0.8, ...}
    
    L1 记忆（分析上下文记忆）：
      - 分析时从 SQLite 缓存检索该股票上次的判断结果
      - 将上次分析（方向/置信度/理由）注入 prompt
      - 让 LLM 判断有连贯性，避免反复横跳
      - 与 LLM 缓存共用 SQLite，无需额外存储
    """
    
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None, enable_l1_memory: bool = True):
        """
        参数：
          provider: LLM 提供商 ("openai", "glm", "deepseek")
            如果为 None，则从环境变量 DEFAULT_LLM_PROVIDER 读取
          model: 模型名称
            如果为 None，则使用该提供商的默认模型
          enable_l1_memory: 是否启用 L1 记忆（默认 True）
        """
        self.provider = provider
        self.model = model
        self.prompt_version = "v2.0"  # 结构化多维打分框架
        self.enable_l1_memory = enable_l1_memory
        
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
            result = self._analyze_with_llm(symbol, date, news_list)
            # L1 记忆：分析结果已自动保存到 SQLite 缓存（_save_cache）
            # 下次分析时通过 _build_user_message 从缓存加载上次判断
            return result
        except Exception as e:
            print(f"[LLMAnalystAgent] LLM 调用失败：{e}")
            raise
    
    def _analyze_with_llm(self, symbol: str, date: str, news_list: List[Dict]) -> Dict:
        """
        使用 LLM 客户端分析新闻（带缓存）。
        """
        # 构建 prompt（system = 固定框架，user = 每次变化的新闻数据）
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message(symbol, date, news_list)
        
        # 检查缓存（可复现性：相同输入直接返回缓存结果）
        cache_key = self._compute_cache_key(symbol, date, system_prompt, user_message)
        cached = self._load_cache(cache_key)
        if cached is not None:
            print(f"  [LLM Cache] 命中缓存: {symbol} @ {date}")
            return cached
        
        # 调用 LLM 客户端
        response = self.llm_client.chat_json(
            message=user_message,
            system_prompt=system_prompt,
            temperature=0.1,  # 低温保证可复现性
            max_tokens=2000,
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
    
    def _build_system_prompt(self) -> str:
        """返回固定的分析框架 prompt（见模块级常量 _SYSTEM_PROMPT）。"""
        return _SYSTEM_PROMPT

    def _build_user_message(self, symbol: str, date: str, news_list: List[Dict]) -> str:
        """
        构建 user message（每次调用变化的新闻数据）。
        如果启用 L1 记忆，会从 SQLite 缓存加载该股票上次的分析结果。
        """
        # 格式化新闻列表（处理可能的 NaN 值）
        news_lines = []
        for i, news in enumerate(news_list[-10:], 1):  # 最多取最近 10 条新闻
            dt = news.get('datetime', 'Unknown')
            title = news.get('title', 'No title')
            summary = news.get('summary', '')
            source = news.get('source', 'Unknown')
            # 处理 summary 可能是 NaN 的情况
            if pd.isna(summary) or not isinstance(summary, str):
                summary = ''
            summary_text = summary[:200] if summary else 'No summary'
            news_lines.append(
                f"[{i}] [{source}] {dt}\n"
                f"    标题：{title}\n"
                f"    摘要：{summary_text}"
            )

        news_text = "\n\n".join(news_lines) if news_lines else "（无新闻）"

        # L1 记忆：从 SQLite 缓存加载上次分析结果
        memory_text = ""
        if self.enable_l1_memory:
            cache_db = get_cache_db()
            prev = cache_db.get_previous_analysis(symbol, date, self.model)
            if prev:
                reasons_str = "；".join(prev.get("reasons", [])[:2]) if prev.get("reasons") else "无"
                memory_text = f"""

## 你上次对 {symbol} 的分析（{prev['analysis_date']}）

- 方向：{prev['direction']}
- 置信度：{prev['confidence']:.2f}
- 理由：{reasons_str}

请结合最新新闻，给出新的判断。如果判断发生变化，请说明原因。"""

        return f"请分析 {symbol} 在 {date} 的新闻：{memory_text}\n\n## 新闻内容\n\n{news_text}"

    # ==================== LLM 输出缓存（SQLite）====================

    def _compute_cache_key(self, symbol: str, date: str, system_prompt: str, user_message: str) -> tuple:
        """
        计算缓存 key（用于 SQLite 查询）。
        返回 (symbol, date, model) 三元组。
        """
        return (symbol, date, self.model)

    def _load_cache(self, cache_key: tuple) -> Optional[Dict]:
        """从 SQLite 数据库加载 LLM 缓存。"""
        symbol, date, model = cache_key
        cache_db = get_cache_db()
        return cache_db.get_cache(symbol, date, model)

    def _save_cache(self, cache_key: tuple, data: Dict):
        """将 LLM 输出保存到 SQLite 数据库。"""
        symbol, date, model = cache_key
        cache_db = get_cache_db()
        
        # 添加 model 字段到 data
        data_to_save = data.copy()
        data_to_save["model"] = model
        
        cache_db.save_cache(data_to_save)


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
      news_df: 新闻 DataFrame（由 fetch_news 返回，包含所有股票）
      provider: LLM 提供商 ("openai", "glm", "deepseek")
      model: 模型名称
    
    返回：
      LLM 分析结果字典
    """
    # 获取该股票在时点对齐的新闻
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
            "datetime": "2023-06-17 09:00:00",
            "title": "Apple reports strong earnings",
            "summary": "Apple beat expectations with Q2 revenue of $90B",
            "source": "Finnhub",
            "sentiment_score": 0.8,
        }
    ]
    
    result = agent.analyze("AAPL", "2023-06-17", news_list)
    print(f"分析结果：{json.dumps(result, indent=2, ensure_ascii=False)}")
