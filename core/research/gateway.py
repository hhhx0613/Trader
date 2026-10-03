"""Snapshot 的只读审计入口 / Read-only, audited access to frozen snapshots.

未来 Agent 只能通过此入口读取冻结事实。每次读取必须精确匹配 snapshot_id、
symbol 与 as_of，并在账本写入查询审计记录；该模块不暴露 Provider、文件系统
或数据库句柄给 Agent。

Future agents may read frozen facts only through this gateway. Every read must
match snapshot_id, symbol, and as_of exactly and is recorded in the ledger. The
module never exposes providers, filesystem paths, or database handles to agents.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .contracts import EvidenceCard, ResearchSnapshot
from .store import ResearchLedger


class DataGateway:
    """执行 Snapshot 边界检查与读取审计 / Enforce Snapshot boundaries and audit reads."""
    def __init__(self, ledger: ResearchLedger) -> None:
        self.ledger = ledger

    def evidence_for_symbol(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> list[EvidenceCard]:
        snapshot = self.ledger.get_snapshot(snapshot_id)
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in snapshot.symbols:
            raise ValueError(f"symbol is not frozen in snapshot: {normalized_symbol}")
        if as_of != snapshot.as_of:
            raise ValueError("gateway as_of must exactly match frozen snapshot")
        cards = self.ledger.list_evidence(snapshot, normalized_symbol)
        if any(card.available_at > snapshot.as_of for card in cards):
            raise ValueError("snapshot contains PIT-invalid evidence")
        self.ledger.record_query(trace_id=snapshot.trace_id, snapshot_id=snapshot.snapshot_id, symbol=normalized_symbol, query_kind="evidence", result_count=len(cards), requested_at=datetime.now(timezone.utc))
        return cards
