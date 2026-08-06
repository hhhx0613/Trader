"""
核心模块（core）

包含量化交易系统的核心组件：
- base_engine: 回测引擎基类（买卖撮合、风控、净值记录的公共逻辑）
- backtest_engine: 单股票回测引擎
- multi_stock_engine: 多股票回测引擎（Top-K 等权 + 调仓）
- data: 数据采集子模块（市场数据、新闻数据、情绪分析）
- indicators: 技术指标计算
- strategy: 规则策略（信号生成）
- risk_manager: 风控模块
- recorder: 交易记录
- config: 配置参数
"""

from . import config
from . import base_engine
from . import backtest_engine
from . import multi_stock_engine
from . import data
from . import indicators
from . import strategy
from . import risk_manager
from . import recorder

__all__ = [
    "config",
    "base_engine",
    "backtest_engine",
    "multi_stock_engine",
    "data",
    "indicators",
    "strategy",
    "risk_manager",
    "recorder",
]
