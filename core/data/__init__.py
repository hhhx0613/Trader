"""
数据采集子模块（core.data）

包含所有外部数据采集组件：
- market_data: K 线数据采集（OHLCV）
- news_data: 新闻数据采集
- sentiment: 情绪分析
"""

from . import market_data
from . import news_data
from . import sentiment

__all__ = [
    "market_data",
    "news_data",
    "sentiment",
]
