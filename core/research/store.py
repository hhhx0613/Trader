"""追加式 SQLite 审计账本 / Append-only SQLite audit ledger.

账本负责对象元数据、对象关系及 Gateway 查询记录；原始正文留在 RawPayloadStore。
迁移显式版本化，并启用 WAL 与外键。trace 完成后不允许继续写入，以避免回放
时历史事实被覆盖。本模块不解释研究内容，也不执行数据库外的 I/O。

The ledger stores object metadata, relations, and Gateway query records while raw
content remains in RawPayloadStore. Migrations are versioned and WAL/foreign keys
are enabled. A completed trace becomes unwritable so replay cannot observe
overwritten history. This module neither interprets research nor performs external I/O.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import EvidenceCard, ResearchSnapshot


OBJECT_TABLES = (
    "traces", "snapshots", "evidence_cards", "claim_cards", "research_packets",
    "portfolio_intents", "intent_constraints", "target_portfolios",
    "risk_projected_portfolios", "order_plans", "deferred_trades", "fills",
    "outcome_cards", "review_labels",
)


class ResearchLedger:
    """保存不可变契约载荷 / Store immutable contract payloads.

    允许运行中的 trace 追加新对象；完成后的 trace 只能读取。该边界在存储层
    强制执行，而非依赖调用者约定。

    Running traces may append objects; completed traces are read-only. The storage
    layer enforces this boundary rather than relying on caller convention.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = 1").fetchone():
                return
            conn.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT)")
            for table in OBJECT_TABLES[1:]:
                conn.execute(
                    f"CREATE TABLE {table} (object_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(trace_id), payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                conn.execute(f"CREATE INDEX idx_{table}_trace_id ON {table}(trace_id)")
            conn.execute("CREATE TABLE gateway_queries (query_id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL REFERENCES traces(trace_id), snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, query_kind TEXT NOT NULL, requested_at TEXT NOT NULL, result_count INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))

    def start_trace(self, trace_id: str, created_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO traces(trace_id, status, created_at) VALUES (?, 'running', ?)", (trace_id, created_at.isoformat()))

    def complete_trace(self, trace_id: str, completed_at: datetime) -> None:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE traces SET status='completed', completed_at=? WHERE trace_id=? AND status='running'", (completed_at.isoformat(), trace_id))
            if cursor.rowcount != 1:
                raise ValueError(f"trace cannot be completed: {trace_id}")

    def append_snapshot(self, snapshot: ResearchSnapshot) -> None:
        self._append("snapshots", snapshot.snapshot_id, snapshot.trace_id, snapshot.model_dump(mode="json"), snapshot.created_at)

    def append_evidence(self, evidence: EvidenceCard) -> None:
        self._append("evidence_cards", evidence.evidence_id, evidence.trace_id, evidence.model_dump(mode="json"), evidence.created_at)

    def _append(self, table: str, object_id: str, trace_id: str, payload: dict[str, Any], created_at: datetime) -> None:
        if table not in OBJECT_TABLES[1:]:
            raise ValueError(f"unsupported ledger table: {table}")
        with self._connect() as conn:
            trace = conn.execute("SELECT status FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            if trace is None or trace["status"] != "running":
                raise ValueError(f"trace is not writable: {trace_id}")
            conn.execute(f"INSERT INTO {table}(object_id, trace_id, payload, created_at) VALUES (?, ?, ?, ?)", (object_id, trace_id, self._dump(payload), created_at.isoformat()))

    def get_snapshot(self, snapshot_id: str) -> ResearchSnapshot:
        row = self._get("snapshots", snapshot_id)
        return ResearchSnapshot.model_validate_json(row["payload"])

    def get_evidence(self, evidence_id: str) -> EvidenceCard:
        row = self._get("evidence_cards", evidence_id)
        return EvidenceCard.model_validate_json(row["payload"])

    def list_evidence(self, snapshot: ResearchSnapshot, symbol: str) -> list[EvidenceCard]:
        return [self.get_evidence(item) for item in snapshot.evidence_ids if self.get_evidence(item).symbol == symbol]

    def record_query(self, *, trace_id: str, snapshot_id: str, symbol: str, query_kind: str, result_count: int, requested_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO gateway_queries(trace_id, snapshot_id, symbol, query_kind, requested_at, result_count) VALUES (?, ?, ?, ?, ?, ?)", (trace_id, snapshot_id, symbol, query_kind, requested_at.isoformat(), result_count))

    def _get(self, table: str, object_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE object_id=?", (object_id,)).fetchone()
        if row is None:
            raise KeyError(f"{table} does not contain {object_id}")
        return row
