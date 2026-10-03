"""
主线数据采集层（core.data）—— fetch -> raw 正文库 -> EvidenceCard -> Agent

- news.py    : 新闻采集器（网格段 + coverage 台账，逐条正文落 news.db）
- market.py  : 行情采集器（批次落 market.db；特征卡由程序算指标产出）
- filings.py : SEC EDGAR 披露采集器（acceptance 时间为权威可得时间）

旧基线的 CSV 网格缓存采集器（news_data/market_data/sentiment）已移入
core/legacy/，仅供 run_backtest/PPO 等对照路径使用，主线禁止 import。
"""

from . import filings
from . import market
from . import news

__all__ = [
    "filings",
    "market",
    "news",
]
