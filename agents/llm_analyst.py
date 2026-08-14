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
import math
import hashlib
import pandas as pd
from typing import Dict, List, Optional
from pathlib import Path

# 支持直接运行本文件（python agents/llm_analyst.py）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import utils.logger as log_utils
logger = log_utils.get_logger(__name__)

from core.data.news_data import get_news_at_date
from core.data.llm_cache_db import get_cache_db
from utils.llm_client import LLMClient

# LLM 输出缓存目录
_LLM_CACHE_DIR = Path(__file__).parent.parent / "core" / "data" / "cache" / "llm"
_LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ==================== Prompt 文件加载 ====================
# 设计：
#   - prompt 按 agent 分目录：agents/prompts/{agent_name}/*.txt
#   - prompt_version 由该 agent 所有 prompt 文件内容的 hash 自动派生
#   - 改任何文件 → hash 变 → 该 agent 的缓存自动失效
#   - 调仓日按 ISO 周日历锚定 → 记忆链与回测窗口解耦 → 换窗口不会级联雪崩

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# composite_score 聚合的压缩尺度：raw 求和 → tanh(raw / _COMPOSITE_SCALE)
# 唯一待标定旋钮（Plan.md 假设 7）：单条实质新闻 score≈4 时给出约 0.6 的强度，
# 两条同向叠加接近饱和。改此值不影响 prompt_version（不参与缓存 key）。
_COMPOSITE_SCALE = 6.0


def load_prompts(agent_name: str) -> tuple[dict, str]:
    """
    加载某 agent 的所有 prompt 文件，返回 (内容字典, 版本标识)。
    
    参数：
      agent_name: agent 名称，对应 prompts/{agent_name}/ 目录
    
    返回：
      (prompts_dict, version)
      - prompts_dict: {文件名stem: 内容}，如 {"system": "...", "user": "..."}
      - version: 所有文件内容的 md5 hash 前 8 位
    
    设计：
      - 版本自动派生，改任何文件 → hash 变 → 缓存自动失效
      - 每个 agent 独立版本，互不影响
    """
    prompts_dir = _PROMPTS_DIR / agent_name
    if not prompts_dir.exists():
        raise FileNotFoundError(f"Prompt 目录不存在：{prompts_dir}")
    
    prompts = {}
    hash_parts = []
    
    # 按文件名排序加载，保证 hash 稳定
    for f in sorted(prompts_dir.glob("*.txt")):
        content = f.read_text(encoding="utf-8")
        prompts[f.stem] = content
        hash_parts.append(content)
    
    # 版本 = 所有文件内容的 hash，自动派生
    version = hashlib.md5("|".join(hash_parts).encode()).hexdigest()[:8]
    return prompts, version


# Analyst agent 的 prompt
_ANALYST_PROMPTS, _ANALYST_PROMPT_VERSION = load_prompts("analyst")
_SYSTEM_PROMPT = _ANALYST_PROMPTS["system"]
_USER_TEMPLATE = _ANALYST_PROMPTS["user"]
_MEMORY_TEMPLATE = _ANALYST_PROMPTS["memory"]


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
        # prompt_version 由该 agent 所有 prompt 文件内容的 hash 自动派生
        # 改任何文件 → hash 变 → 缓存自动失效
        self.prompt_version = _ANALYST_PROMPT_VERSION
        self.enable_l1_memory = enable_l1_memory

        # 初始化 LLM 客户端
        try:
            self.llm_client = LLMClient(provider=provider, model=model)
            # 更新为实际使用的模型
            self.model = self.llm_client.model
            logger.info(f"LLMAnalystAgent initialized: model={self.model}, prompt_version={self.prompt_version}")
        except Exception as e:
            raise RuntimeError(f"LLM 客户端初始化失败：{e}")

    def analyze(
        self,
        symbol: str,
        date: str,
        news_list: List[Dict],
        price_series: Optional[pd.Series] = None,
    ) -> Dict:
        """
        分析新闻，生成结构化观点。

        参数：
          symbol: 股票代码（如 "AAPL"）
          date: 分析日期（格式 "YYYY-MM-DD"）
          news_list: 新闻列表（由 get_news_at_date 返回）
          price_series: 该股票的收盘价序列（index=日期字符串或 Timestamp，value=float）。
                        用于 L1 记忆注入「上次判断后的实际市场反应」。
                        为 None 时降级为旧的一致性记忆（不注入收益反馈）。

        返回：
          {
            "symbol": "AAPL",
            "per_news": [{"impact": 1, "expectation_gap": 0, "timeliness": 1.0, "certainty": 1.0, "score": 1.0, ...}],
            "composite_score": 0.75,  # 代码计算：tanh(Σ score / _COMPOSITE_SCALE)
            "direction": "bullish",   # 代码计算：composite_score > 0.1 → bullish
            "confidence": 0.8,        # LLM 评估：基于信息质量
            "key_factors": ["...", "..."],
            "reasons": ["...", "..."],
            "analysis_date": "2023-06-15"
          }
        """
        # 调用 LLM API
        try:
            result = self._analyze_with_llm(symbol, date, news_list, price_series)
            return result
        except Exception as e:
            print(f"[LLMAnalystAgent] LLM 调用失败：{e}")
            raise

    def _analyze_with_llm(self, symbol: str, date: str, news_list: List[Dict],
                          price_series: Optional[pd.Series] = None) -> Dict:
        """
        使用 LLM 客户端分析新闻（带缓存）。
        """
        # 构建 prompt（system = 固定框架，user = 每次变化的新闻数据 + L1 记忆）
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message(symbol, date, news_list, price_series)

        # 检查缓存（可复现性：相同输入直接返回缓存结果）
        cache_key = self._compute_cache_key(symbol, date)
        cached = self._load_cache(cache_key)
        if cached is not None:
            print(f"  [LLM Cache] 命中缓存: {symbol} @ {date}")
            return cached

        # 调用 LLM 客户端
        response = self.llm_client.chat_json(
            message=user_message,
            system_prompt=system_prompt,
            temperature=0.1,  # 低温保证可复现性
        )

        # 检查是否有错误
        if "error" in response:
            raise RuntimeError(f"LLM 返回错误：{response['error']}")

        # 代码计算 score / composite_score / direction（LLM 不擅长精确数学）
        self._compute_scores(response)

        # 添加元数据
        response["symbol"] = symbol
        response["analysis_date"] = date
        response["prompt_version"] = self.prompt_version

        # 写入缓存
        self._save_cache(cache_key, response)

        return response
    
    def _build_system_prompt(self) -> str:
        """返回分析框架 prompt（从 prompts/ 目录加载）。"""
        return _SYSTEM_PROMPT

    def _compute_scores(self, response: Dict) -> None:
        """
        根据 LLM 返回的 per_news 维度打分，计算 score / composite_score / direction。
        直接在 response 上修改（原地更新）。

        设计（Plan.md 假设 7）：
          - LLM 只负责语义评估（四维打分），不擅长精确数学
          - composite_score 是「求和 + tanh 压缩」得到的有符号连续强度信号：
                raw = Σ_i (impact_i + gap_i) * timeliness_i * certainty_i
                composite_score = tanh(raw / _COMPOSITE_SCALE)  ∈ (-1, 1)
          - 用求和而非加权平均：中性噪音 score≈0 不改变求和 → 天然不稀释；
            多条同向新闻自动叠加；单条极端事件自动主导；无需任何权重方案，
            certainty 仅作乘数用一次，消除双重计权。
          - direction 由 composite_score 阈值决定，退居次要（仅供选股门槛与日志）
        """
        per_news = response.get("per_news", [])

        if not per_news:
            response["composite_score"] = 0.0
            response["direction"] = "neutral"
            return

        # 计算每条新闻的 score，并直接求和为 raw（中性 score≈0 不贡献，不稀释）
        raw = 0.0
        for news in per_news:
            impact = news.get("impact", 0)
            exp_gap = news.get("expectation_gap", 0)
            timeliness = news.get("timeliness", 1.0)
            certainty = news.get("certainty", 1.0)

            score = (impact + exp_gap) * timeliness * certainty
            news["score"] = round(score, 4)
            raw += score

        # 求和 → tanh 压缩到 [-1, 1]
        composite_score = math.tanh(raw / _COMPOSITE_SCALE)
        response["composite_score"] = round(composite_score, 4)

        # direction 由 composite_score 阈值决定
        if composite_score > 0.1:
            response["direction"] = "bullish"
        elif composite_score < -0.1:
            response["direction"] = "bearish"
        else:
            response["direction"] = "neutral"

    def _build_user_message(self, symbol: str, date: str, news_list: List[Dict],
                            price_series: Optional[pd.Series] = None) -> str:
        """
        构建 user message（每次调用变化的新闻数据）。
        如果启用 L1 记忆，会从 SQLite 缓存加载该股票上次的分析结果，
        并根据 price_series 计算「上次判断后的实际市场反应」注入 prompt。
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

        # L1 记忆：从 SQLite 缓存加载上次分析结果（同 prompt_version 内）
        memory_text = ""
        if self.enable_l1_memory:
            cache_db = get_cache_db()
            prev = cache_db.get_previous_analysis(
                symbol, date, self.model, self.prompt_version
            )
            if prev:
                reasons_str = "；".join(prev.get("reasons", [])[:2]) if prev.get("reasons") else "无"
                prev_date = prev['analysis_date']
                prev_dir = prev['direction']
                prev_conf = prev['confidence']

                # 计算「上次判断后的实际市场反应」（PIT 安全：只用 ≤ current_date 的历史价）
                outcome_text = ""
                if price_series is not None:
                    try:
                        # 用 asof 兼容不同 index 类型（str / Timestamp）
                        close_prev = price_series.asof(prev_date)
                        close_curr = price_series.asof(date)
                        # 容错：若 asof 返回 NaN 或 prev_date 在序列之前，跳过
                        if pd.notna(close_prev) and pd.notna(close_curr) and close_prev > 0:
                            realized = close_curr / close_prev - 1.0
                            # 命中判定：bullish+涨 / bearish+跌 → 正确；neutral → 未验证
                            if prev_dir == "neutral":
                                verdict = "未验证（上次弃权）"
                            elif (prev_dir == "bullish" and realized > 0) or \
                                 (prev_dir == "bearish" and realized < 0):
                                verdict = "判断正确"
                            else:
                                verdict = "判断错误"
                            outcome_text = (
                                f"之后实际收益 **{realized:+.1%}**，**{verdict}**。"
                            )
                    except Exception as e:
                        logger.debug(f"[L1 Memory] 计算实际收益失败 {symbol} {prev_date}->{date}: {e}")

                # 构建 outcome 行（有实际收益时加上）
                outcome_line = f"- {outcome_text}" if outcome_text else ""

                # 用模板填充记忆块
                memory_text = _MEMORY_TEMPLATE.format(
                    symbol=symbol,
                    prev_date=prev_date,
                    prev_dir=prev_dir,
                    prev_conf=f"{prev_conf:.2f}",
                    reasons_str=reasons_str,
                    outcome_line=outcome_line,
                )

        # 用模板填充最终 user message
        return _USER_TEMPLATE.format(
            symbol=symbol,
            date=date,
            memory_text=memory_text,
            news_text=news_text,
        )

    # ==================== LLM 输出缓存（SQLite）====================

    def _compute_cache_key(self, symbol: str, date: str) -> tuple:
        """
        计算缓存 key（用于 SQLite 查询）。
        返回 (symbol, date, model, prompt_version) 四元组。

        设计：
          - prompt_version 由该 agent 所有 prompt 文件内容的 hash 自动派生
          - 调仓日按 ISO 周日历锚定 → 记忆链与回测窗口解耦 → 换窗口不会级联雪崩
          - 记忆内容与实际收益都是「被这四元组唯一决定的确定性函数」，不进 key
        """
        return (symbol, date, self.model, self.prompt_version)

    def _load_cache(self, cache_key: tuple) -> Optional[Dict]:
        """从 SQLite 数据库加载 LLM 缓存。"""
        symbol, date, model, prompt_version = cache_key
        cache_db = get_cache_db()
        return cache_db.get_cache(symbol, date, model, prompt_version)

    def _save_cache(self, cache_key: tuple, data: Dict):
        """将 LLM 输出保存到 SQLite 数据库。"""
        symbol, date, model, prompt_version = cache_key
        cache_db = get_cache_db()

        # 添加 model 字段到 data
        data_to_save = data.copy()
        data_to_save["model"] = model
        data_to_save["prompt_version"] = prompt_version

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
    agent = LLMAnalystAgent(provider=provider, model=model, enable_l1_memory=False)
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
