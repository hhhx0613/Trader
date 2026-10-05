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
from typing import Any, Iterable

from .contracts import (
    ClaimCard,
    EvidenceCard,
    PortfolioIntent,
    ResearchPacket,
    ResearchSnapshot,
    ThesisBook,
)


OBJECT_TABLES = (
    # 表集合包含后续阶段对象；阶段 4-6 启动时只需补契约类与 typed 方法，不再改表结构。
    "traces", "snapshots", "evidence_cards", "claim_cards", "research_packets",
    "thesis_books", "portfolio_intents", "intent_constraints", "target_portfolios",
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
        # 迁移按版本递增；已有库只补新增版本，不重建历史表。
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                conn.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT)")
                for table in OBJECT_TABLES[1:]:
                    conn.execute(
                        f"CREATE TABLE {table} (object_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(trace_id), payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                    )
                    conn.execute(f"CREATE INDEX idx_{table}_trace_id ON {table}(trace_id)")
                conn.execute("CREATE TABLE gateway_queries (query_id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL REFERENCES traces(trace_id), snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, query_kind TEXT NOT NULL, requested_at TEXT NOT NULL, result_count INTEGER NOT NULL)")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))")
            if 2 not in applied:
                # ThesisBook 未列入 v1 建表清单；契约冻结时补表，IF NOT EXISTS 保证新旧库一致。
                conn.execute("CREATE TABLE IF NOT EXISTS thesis_books (object_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(trace_id), payload TEXT NOT NULL, created_at TEXT NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_thesis_books_trace_id ON thesis_books(trace_id)")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (2, datetime('now'))")
            if 3 not in applied:
                # 全局卡注册表（"一条事实一张卡"）：按 content_hash+symbol 查已建卡片，
                # 后续轮次的 Snapshot 复用旧卡 ID 而不重复建行。
                conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_content_hash ON evidence_cards(json_extract(payload, '$.content_hash'))")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (3, datetime('now'))")
            if 4 not in applied:
                # freeze_snapshot 按窗口选卡：datetime() 归一不同时区偏移的 ISO 串后
                # 可比较/排序；symbol 随小表扫描过滤，不另建索引。
                conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_available_at ON evidence_cards(datetime(json_extract(payload, '$.available_at')))")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (4, datetime('now'))")
            if 5 not in applied:
                # 原 gateway_queries 记录程序读；tool_calls 额外保留模型请求的参数与返回卡片，
                # 使重放可区分“模型要求什么”和“Gateway 实际允许它看到什么”。
                conn.execute("CREATE TABLE tool_calls (call_id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL REFERENCES traces(trace_id), snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, node TEXT NOT NULL, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, requested_at TEXT NOT NULL, result_count INTEGER NOT NULL, evidence_ids TEXT NOT NULL)")
                conn.execute("CREATE INDEX idx_tool_calls_trace_id ON tool_calls(trace_id)")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (5, datetime('now'))")

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

    # 以下 typed append/get 方法与契约类一一对应；禁止绕过它们直接拼 SQL 写入对象表。

    def append_claim(self, claim: ClaimCard) -> None:
        self._append("claim_cards", claim.claim_id, claim.trace_id, claim.model_dump(mode="json"), claim.created_at)

    def get_claim(self, claim_id: str) -> ClaimCard:
        return ClaimCard.model_validate_json(self._get("claim_cards", claim_id)["payload"])

    def append_packet(self, packet: ResearchPacket) -> None:
        self._append("research_packets", packet.packet_id, packet.trace_id, packet.model_dump(mode="json"), packet.created_at)

    def get_packet(self, packet_id: str) -> ResearchPacket:
        return ResearchPacket.model_validate_json(self._get("research_packets", packet_id)["payload"])

    def append_thesis_book(self, book: ThesisBook) -> None:
        self._append("thesis_books", book.thesis_book_id, book.trace_id, book.model_dump(mode="json"), book.created_at)

    def get_thesis_book(self, thesis_book_id: str) -> ThesisBook:
        return ThesisBook.model_validate_json(self._get("thesis_books", thesis_book_id)["payload"])

    def append_intent(self, intent: PortfolioIntent) -> None:
        self._append("portfolio_intents", intent.intent_id, intent.trace_id, intent.model_dump(mode="json"), intent.created_at)

    def get_intent(self, intent_id: str) -> PortfolioIntent:
        return PortfolioIntent.model_validate_json(self._get("portfolio_intents", intent_id)["payload"])

    # 组合/订单/复盘契约的 append/get 在阶段 4-6 定义对应契约类时一并补，不提前留空方法。

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

    def find_evidence_by_content(self, content_hash: str, symbol: str) -> EvidenceCard | None:
        """全局卡查找：同一内容在同一标的下只应有一张卡。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM evidence_cards WHERE json_extract(payload, '$.content_hash') = ? AND json_extract(payload, '$.symbol') = ?",
                (content_hash, symbol.upper()),
            ).fetchone()
        return EvidenceCard.model_validate_json(row["payload"]) if row else None

    def list_evidence(self, snapshot: ResearchSnapshot, symbol: str) -> list[EvidenceCard]:
        """一次查询取回本 Snapshot 下该标的的全部卡。

        旧写法逐 ID 往返整池两遍（过滤一次、取值一次）再丢弃九成，Gateway 每个
        读工具都走这条路，周度全池规模下会直接跑不动；改为在 SQL 里同时完成
        「限定本 Snapshot 卡集」与「按标的过滤」，只反序列化真正命中的卡。缺失
        的卡 ID 意味着账本损坏，单独数一次后抛错，不能静默少返证据。

        排序固定为可得时间倒序：harness 的字符预算按列表顺序逐张截断，升序会让
        旧闻先吃掉预算、最新事件被截在窗口外；get_news_batch 的“最新优先”口径
        在这里统一，而不是各自排一次。
        """
        if not snapshot.evidence_ids:
            return []
        ids = json.dumps(list(snapshot.evidence_ids))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.payload FROM evidence_cards e, json_each(?) AS ids "
                "WHERE e.object_id = ids.value AND json_extract(e.payload, '$.symbol') = ? "
                "ORDER BY datetime(json_extract(e.payload, '$.available_at')) DESC, e.object_id DESC",
                (ids, symbol.strip().upper()),
            ).fetchall()
            missing = conn.execute(
                "SELECT count(*) FROM json_each(?) AS ids "
                "LEFT JOIN evidence_cards e ON e.object_id = ids.value WHERE e.object_id IS NULL",
                (ids,),
            ).fetchone()[0]
        if missing:
            raise KeyError(f"snapshot references {missing} missing evidence card(s)")
        return [EvidenceCard.model_validate_json(row["payload"]) for row in rows]

    def select_evidence(self, *, symbols: Iterable[str], available_from: datetime, available_to: datetime) -> list[EvidenceCard]:
        """卡片池选卡：按标的与可得时间闭窗口 [from, to] 筛选，供 freeze_snapshot 使用。

        比较走 datetime() UTC 归一，不受写入时的时区偏移写法影响；结果按
        可得时间升序 + object_id 稳定排序，保证同一库同一窗口重放选出同一清单。
        """
        pool = tuple(str(symbol).strip().upper() for symbol in symbols)
        if not pool or any(not symbol for symbol in pool):
            raise ValueError("select_evidence requires at least one non-empty symbol")
        placeholders = ",".join("?" for _ in pool)
        available = "datetime(json_extract(payload, '$.available_at'))"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM evidence_cards WHERE json_extract(payload, '$.symbol') IN ({placeholders}) "
                f"AND {available} >= datetime(?) AND {available} <= datetime(?) "
                f"ORDER BY {available}, object_id",
                (*pool, available_from.isoformat(), available_to.isoformat()),
            ).fetchall()
        return [EvidenceCard.model_validate_json(row["payload"]) for row in rows]

    def record_query(self, *, trace_id: str, snapshot_id: str, symbol: str, query_kind: str, result_count: int, requested_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO gateway_queries(trace_id, snapshot_id, symbol, query_kind, requested_at, result_count) VALUES (?, ?, ?, ?, ?, ?)", (trace_id, snapshot_id, symbol, query_kind, requested_at.isoformat(), result_count))

    def record_tool_call(self, *, trace_id: str, snapshot_id: str, symbol: str, node: str,
                         tool_name: str, arguments: dict[str, Any], result_count: int,
                         evidence_ids: list[str], requested_at: datetime) -> None:
        """追加模型经 harness 发起的受控补查；trace 完成后同样不可修改。"""
        with self._connect() as conn:
            trace = conn.execute("SELECT status FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            if trace is None or trace["status"] != "running":
                raise ValueError(f"trace is not writable: {trace_id}")
            conn.execute("INSERT INTO tool_calls(trace_id, snapshot_id, symbol, node, tool_name, arguments, requested_at, result_count, evidence_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (trace_id, snapshot_id, symbol, node, tool_name, self._dump(arguments),
                          requested_at.isoformat(), result_count, self._dump(evidence_ids)))

    def list_tool_calls(self, trace_id: str) -> list[dict[str, Any]]:
        """重放审计：返回该 trace 的全部模型工具请求记录，arguments/evidence_ids 已解 JSON。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tool_calls WHERE trace_id=? ORDER BY call_id", (trace_id,)).fetchall()
        return [{"node": row["node"], "tool_name": row["tool_name"], "symbol": row["symbol"],
                 "arguments": json.loads(row["arguments"]), "result_count": row["result_count"],
                 "evidence_ids": json.loads(row["evidence_ids"])} for row in rows]

    def _get(self, table: str, object_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE object_id=?", (object_id,)).fetchone()
        if row is None:
            raise KeyError(f"{table} does not contain {object_id}")
        return row
