"""冻结 PIT 合格事实快照 / Build frozen, point-in-time-safe research snapshots.

SnapshotBuilder 接收已获取的新闻、披露和行情记录，先校验 available_at，再去重、
保存原始载荷和 EvidenceCard，最后冻结股票池、来源版本与账户状态。它不联网，
不调用 Agent，也不会生成交易意图；这些职责属于后续阶段。

SnapshotBuilder accepts already acquired news, filing, and market records. It
validates available_at, deduplicates, persists raw payloads and EvidenceCards,
then freezes symbols, source versions, and account state. It does not use the
network, call agents, or generate intents; those belong to later stages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .contracts import EvidenceCard, ResearchSnapshot
from .providers import PITValidationError, normalize_record
from .raw import RawPayloadStore
from .store import ResearchLedger


class SnapshotBuilder:
    """离线构建并持久化 Snapshot / Build and persist a Snapshot without network access."""

    def __init__(self, ledger: ResearchLedger, raw_store: RawPayloadStore) -> None:
        self.ledger = ledger
        self.raw_store = raw_store

    def build(self, *, trace_id: str, as_of: datetime, symbols: Iterable[str], source_versions: dict[str, str], account_state: dict[str, Any], records: Iterable[dict[str, Any]]) -> ResearchSnapshot:
        self.ledger.start_trace(trace_id, as_of)
        evidence: list[EvidenceCard] = []
        seen: set[str] = set()
        for record in records:
            kind = record.get("kind")
            if kind not in {"news", "filing", "market"}:
                raise PITValidationError("invalid_kind", "record kind must be news, filing, or market")
            normalized = normalize_record(record, kind=kind, as_of=as_of)
            if normalized["content_hash"] in seen:
                continue
            seen.add(normalized["content_hash"])
            raw = self.raw_store.put(normalized, source=normalized["source"], received_at=as_of)
            card = EvidenceCard(
                trace_id=trace_id, kind=kind, symbol=normalized["symbol"], published_at=normalized.get("published_at"),
                available_at=normalized["available_at"], source=normalized["source"], raw=raw,
                content_hash=normalized["content_hash"], canonical_url=normalized.get("url"), metadata={"provider_id": normalized.get("provider_id")},
            )
            self.ledger.append_evidence(card)
            evidence.append(card)
        snapshot = ResearchSnapshot(trace_id=trace_id, as_of=as_of, symbols=tuple(symbols), source_versions=source_versions, evidence_ids=tuple(item.evidence_id for item in evidence), account_state=account_state)
        self.ledger.append_snapshot(snapshot)
        self.ledger.complete_trace(trace_id, as_of)
        return snapshot
