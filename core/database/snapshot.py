"""冻结 PIT 合格事实快照 / Build frozen, point-in-time-safe research snapshots.

SnapshotBuilder 分两个独立入口，对应 Plan.md 的两种节奏：
  - ingest_records：每日/事件驱动。校验 PIT、去重、落正文、建 EvidenceCard
    后关闭本轮 trace，不冻结 Snapshot；新内容建卡，已有卡直接复用。
  - freeze_snapshot：周度调仓/临时复核。从卡片池按标的与可得时间窗口选卡
    （或显式传 ID）冻结 Snapshot，不建卡、不碰正文库。
两者都不联网、不调 Agent、不生成交易意图；这些职责属于后续阶段。

SnapshotBuilder exposes two entries matching the planned cadence: ingest_records
validates PIT, deduplicates and creates EvidenceCards daily or event-driven
without freezing; freeze_snapshot selects cards from the pool and freezes a
Snapshot at decision time without ingesting. Neither uses the network, agents,
nor trade intents.

阶段一 ingest_records：记录 -> 卡片（函数标注 文件.函数）

    records 逐条循环
      ├─ kind 闸门                      snapshot.py 自身；非法抛
      │                                 providers.PITValidationError(reasons.ReasonCode.INVALID_KIND)
      ├─ normalize_record               providers.py
      │   ├─ validate_available_at      providers.py；缺时间/无时区/晚于 as_of
      │   │                             → 拒绝，错码来自 reasons.ReasonCode
      │   └─ canonical_content_hash     providers.py；转载 URL 不同也算同一事实
      ├─ seen 集合轮内去重（哈希+标的）    snapshot.py 自身
      ├─ find_evidence_by_content       store.py；命中 -> 复用旧卡，本条结束（不碰正文库）
      ├─ raw_store.put                  raw.py；正文按 kind 入 news/market/filings.db，
      │                                 sha256 主键 INSERT OR IGNORE，返回 RawReference 指针
      └─ EvidenceCard(...) 构造          contracts.py；extra=forbid + symbol 规范化当场把关
         append_evidence                store.py；_append 强制 trace 仍 running 才可写
    开头/结尾 store.start_trace / complete_trace：本阶段无 Snapshot。

阶段二 freeze_snapshot：选卡 -> 冻结（不建卡、不碰正文库）

    选卡二选一（都不传直接 ValueError，拒绝隐式默认窗口）
      ├─ evidence_ids 显式点名          调用方持有（如本轮 ingest 结果）
      └─ available_from 池选            store.select_evidence：标的+可得窗口闭区间，
                                        datetime() UTC 归一，确定性排序
      ↓
    start_trace -> ResearchSnapshot(...)  contracts.py，unique_symbols 把关
              -> append_snapshot         store.py
              -> complete_trace          store.py，此后本轮永久封笔

下游读取（gateway.evidence_for_symbol -> store.get_snapshot/list_evidence）
不在本模块职责内；本模块只是写入侧的编排者。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .contracts import EvidenceCard, ResearchSnapshot
from .providers import PITValidationError, normalize_record
from .raw import RawPayloadStore
from .reasons import ReasonCode
from .store import ResearchLedger


class SnapshotBuilder:
    """建卡与冻结分离的两个入口 / Ingest cards without freezing; freeze by selecting from the pool."""

    def __init__(self, ledger: ResearchLedger, raw_store: RawPayloadStore) -> None:
        self.ledger = ledger
        self.raw_store = raw_store

    def ingest_records(self, *, trace_id: str, as_of: datetime, records: Iterable[dict[str, Any]]) -> list[EvidenceCard]:
        """阶段一：新记录校验、去重、建卡，独立开/关 trace；不冻结 Snapshot。

        全局卡复用：一条事实在每个标的下各一张卡（卡片身份 = 正文哈希 + 标的）；
        PIT 校验在 normalize 完成（复用卡同样受本轮 available_at <= as_of 约束），
        旧卡直接进本轮结果不重复建行。
        """
        self.ledger.start_trace(trace_id, as_of)
        evidence: list[EvidenceCard] = []
        # 轮内去重必须与 find_evidence_by_content 同键（正文哈希 + 标的）：只按哈希去重时，
        # 同一轮里被多个标的引用的同稿只会给第一个标的建卡，其余静默丢弃；下一轮却会
        # 按 (哈希, 标的) 给它们补建——同一事实的建卡数量取决于它恰好和谁排在同一轮。
        seen: set[tuple[str, str]] = set()
        for record in records:
            kind = record.get("kind")
            if kind not in {"news", "filing", "market"}:
                raise PITValidationError(ReasonCode.INVALID_KIND, "record kind must be news, filing, or market")
            normalized = normalize_record(record, kind=kind, as_of=as_of)
            identity = (normalized["content_hash"], normalized["symbol"])
            if identity in seen:
                continue
            seen.add(identity)
            existing = self.ledger.find_evidence_by_content(normalized["content_hash"], normalized["symbol"])
            if existing is not None:
                evidence.append(existing)
                continue
            raw = self.raw_store.put(normalized, source=normalized["source"], received_at=as_of)
            card = EvidenceCard(
                trace_id=trace_id, kind=kind, symbol=normalized["symbol"], published_at=normalized.get("published_at"),
                available_at=normalized["available_at"], source=normalized["source"], raw=raw,
                content_hash=normalized["content_hash"], canonical_url=normalized.get("url"), metadata={"provider_id": normalized.get("provider_id")},
            )
            self.ledger.append_evidence(card)
            evidence.append(card)
        self.ledger.complete_trace(trace_id, as_of)
        return evidence

    def freeze_snapshot(self, *, trace_id: str, as_of: datetime, symbols: Iterable[str], source_versions: dict[str, str], account_state: dict[str, Any],
                        available_from: datetime | None = None, evidence_ids: tuple[str, ...] | None = None) -> ResearchSnapshot:
        """阶段二：选卡冻结 Snapshot；不建卡。

        选卡二选一，不给隐式默认窗口：显式 evidence_ids（本轮刚 ingest 的卡，
        或人工挑选的回放集），或 available_from 走卡片池按标的+可得窗口筛。
        """
        pool = tuple(symbols)
        if evidence_ids is None:
            if available_from is None:
                raise ValueError("freeze_snapshot requires either evidence_ids or available_from")
            cards = self.ledger.select_evidence(symbols=pool, available_from=available_from, available_to=as_of)
            evidence_ids = tuple(card.evidence_id for card in cards)
        self.ledger.start_trace(trace_id, as_of)
        snapshot = ResearchSnapshot(trace_id=trace_id, as_of=as_of, symbols=pool, source_versions=source_versions, evidence_ids=evidence_ids, account_state=account_state)
        self.ledger.append_snapshot(snapshot)
        self.ledger.complete_trace(trace_id, as_of)
        return snapshot
