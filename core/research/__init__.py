"""阶段一研究数据层的公共入口 / Public entry point for the Stage-1 research data layer.

该包只提供 PIT 安全的事实冻结、原始载荷定位和审计读取能力；后续 Agent
必须通过 DataGateway 读取 Snapshot，不能直接访问数据源。本包不调用模型、
不产生交易意图，也不创建订单。

This package freezes point-in-time-safe facts, locates raw payloads, and exposes
audited reads only. Future agents must read snapshots through DataGateway; this
package neither calls models nor produces intents or orders.
"""

from .contracts import (
    ClaimCard,
    EvidenceCard,
    PortfolioIntent,
    ResearchPacket,
    ResearchSnapshot,
    RouteDecision,
    ThesisBook,
)
from .gateway import DataGateway
from .reasons import ReasonCode
from .snapshot import SnapshotBuilder
from .store import ResearchLedger

__all__ = [
    "ClaimCard", "DataGateway", "EvidenceCard", "PortfolioIntent",
    "ReasonCode", "ResearchLedger", "ResearchPacket", "ResearchSnapshot",
    "RouteDecision", "SnapshotBuilder", "ThesisBook",
]
