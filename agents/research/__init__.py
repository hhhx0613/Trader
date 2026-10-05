"""冻结 Snapshot 上的研究节点 / Stateless research nodes over frozen snapshots.

本包只形成带引用的研究结论与显式路由，不读取文件、数据库、Provider 或网络数据；
事实读取必须经由 DataGateway，模型调用必须由调用方注入的 LLMClient 完成。
"""

from .event_agent import EventAgent
from .fundamental_agent import FundamentalAgent
from .graph import PerAssetResearchGraph
from .market_agent import MarketAgent
from .committee import Committee
from .risk_critic import RiskCritic

__all__ = ["Committee", "EventAgent", "FundamentalAgent", "MarketAgent", "PerAssetResearchGraph", "RiskCritic"]
