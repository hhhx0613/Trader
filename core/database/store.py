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
    Fill,
    IntentItem,
    IntentConstraints,
    OrderPlan,
    PortfolioIntent,
    RiskProjectedPortfolio,
    ResearchPacket,
    ResearchSnapshot,
    TargetPortfolio,
    ThesisBook,
    OutcomeCard,
    PositionEpisode,
    ReviewLabel,
)


OBJECT_TABLES = (
    # 表集合包含后续阶段对象；阶段 4-6 启动时只需补契约类与 typed 方法，不再改表结构。
    "traces", "snapshots", "evidence_cards", "claim_cards", "research_packets",
    "thesis_books", "portfolio_intents", "intent_constraints", "target_portfolios",
    "risk_projected_portfolios", "order_plans", "deferred_trades", "fills", "position_episodes",
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
            if 6 not in applied:
                conn.execute("CREATE TABLE IF NOT EXISTS position_episodes (object_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(trace_id), payload TEXT NOT NULL, created_at TEXT NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_position_episodes_trace_id ON position_episodes(trace_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_position_episodes_symbol ON position_episodes(json_extract(payload, '$.symbol'))")
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (6, datetime('now'))")

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

    def fail_trace(self, trace_id: str, completed_at: datetime) -> None:
        """保留失败调度的审计锚点，避免异常后留下可继续写入的 running trace。"""
        with self._connect() as conn:
            cursor = conn.execute("UPDATE traces SET status='failed', completed_at=? WHERE trace_id=? AND status='running'", (completed_at.isoformat(), trace_id))
            if cursor.rowcount != 1:
                raise ValueError(f"trace cannot be failed: {trace_id}")

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

    def get_intent_item(self, intent_item_id: str) -> IntentItem:
        with self._connect() as conn:
            rows = tuple(conn.execute("SELECT payload FROM portfolio_intents ORDER BY created_at DESC"))
        for row in rows:
            intent = PortfolioIntent.model_validate_json(row["payload"])
            for item in intent.items:
                if item.intent_item_id == intent_item_id:
                    return item
        raise KeyError(f"intent item not found: {intent_item_id}")

    def append_constraints(self, constraints: IntentConstraints) -> None:
        self._append("intent_constraints", constraints.intent_id, constraints.trace_id,
                     constraints.model_dump(mode="json"), constraints.created_at)

    def get_constraints(self, intent_id: str) -> IntentConstraints:
        return IntentConstraints.model_validate_json(self._get("intent_constraints", intent_id)["payload"])

    def append_target_portfolio(self, target: TargetPortfolio) -> None:
        self._append("target_portfolios", target.target_portfolio_id, target.trace_id,
                     target.model_dump(mode="json"), target.created_at)

    def get_target_portfolio(self, target_portfolio_id: str) -> TargetPortfolio:
        return TargetPortfolio.model_validate_json(self._get("target_portfolios", target_portfolio_id)["payload"])

    def append_risk_projected_portfolio(self, projected: RiskProjectedPortfolio) -> None:
        self._append("risk_projected_portfolios", projected.projected_portfolio_id, projected.trace_id,
                     projected.model_dump(mode="json"), projected.created_at)

    def get_risk_projected_portfolio(self, projected_portfolio_id: str) -> RiskProjectedPortfolio:
        return RiskProjectedPortfolio.model_validate_json(
            self._get("risk_projected_portfolios", projected_portfolio_id)["payload"])

    def append_order_plan(self, plan: OrderPlan) -> None:
        self._append("order_plans", plan.order_plan_id, plan.trace_id,
                     plan.model_dump(mode="json"), plan.created_at)

    def get_order_plan(self, order_plan_id: str) -> OrderPlan:
        return OrderPlan.model_validate_json(self._get("order_plans", order_plan_id)["payload"])

    def append_fill(self, fill: Fill) -> None:
        self._append("fills", fill.fill_id, fill.trace_id, fill.model_dump(mode="json"), fill.created_at)

    def get_fill(self, fill_id: str) -> Fill:
        return Fill.model_validate_json(self._get("fills", fill_id)["payload"])

    def list_fills_for_order_plan(self, order_plan_id: str) -> tuple[Fill, ...]:
        return tuple(Fill.model_validate_json(row["payload"]) for row in self._find_payloads(
            "fills", "$.order_plan_id", order_plan_id))

    def append_position_episode(self, episode: PositionEpisode) -> None:
        """追加 Episode 快照；同一 episode_id 的 revision 单调递增，旧快照永不覆盖。"""
        object_id = f"{episode.episode_id}:{episode.revision}"
        self._append("position_episodes", object_id, episode.trace_id,
                     episode.model_dump(mode="json"), episode.created_at)

    def get_position_episode(self, episode_id: str) -> PositionEpisode:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM position_episodes WHERE json_extract(payload, '$.episode_id') = ? "
                "ORDER BY CAST(json_extract(payload, '$.revision') AS INTEGER) DESC LIMIT 1", (episode_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"position episode not found: {episode_id}")
        return PositionEpisode.model_validate_json(row["payload"])

    def list_active_position_episodes(self) -> tuple[PositionEpisode, ...]:
        return tuple(episode for episode in self._latest_position_episodes() if episode.status == "active")

    def find_active_position_episode(self, symbol: str) -> PositionEpisode | None:
        symbol = symbol.upper()
        matches = [episode for episode in self.list_active_position_episodes() if episode.symbol == symbol]
        if len(matches) > 1:
            raise ValueError(f"multiple active position episodes for {symbol}")
        return matches[0] if matches else None

    def list_verified_memory_episodes(self) -> tuple[PositionEpisode, ...]:
        """只返回可物化为 Historical Episode Case 的聚合根。"""
        episodes = []
        for episode in self._latest_position_episodes():
            if episode.status != "terminal" or not episode.outcome_card_id or not episode.review_label_id:
                continue
            label = self.get_review_label(episode.review_label_id)
            if label.status == "complete" and label.memory_eligibility == "eligible":
                episodes.append(episode)
        return tuple(episodes)

    def select_verified_memory_episodes(self, *, symbols: tuple[str, ...], regime: str | None,
                                        as_of: datetime, query_profiles: dict[str, dict[str, Any]],
                                        limit_per_symbol: int = 4) -> tuple[dict[str, tuple[PositionEpisode, ...]], dict[str, list[dict[str, Any]]]]:
        """确定性近邻排序；返回审计明细，避免把“检索到什么”藏在 prompt 外。"""
        if limit_per_symbol < 1:
            raise ValueError("episode selection limit must be positive")
        # 决策时点之后才完成的 Outcome/Review 属于未来信息，绝不能进入本轮 prompt。
        verified = self.list_verified_memory_episodes()
        candidates = tuple(
            episode for episode in verified
            if (outcome := self.get_outcome_card(episode.outcome_card_id or "")).observed_at < as_of
            and (label := self.get_review_label(episode.review_label_id or "")).created_at < as_of
        )
        result: dict[str, tuple[PositionEpisode, ...]] = {}
        audit: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            profile = query_profiles.get(symbol, {})
            rows: list[tuple[PositionEpisode, dict[str, int]]] = []
            details = [{"episode_id": episode.episode_id, "score": {}, "total_score": None,
                        "selected": False, "exclusion_reason": "completed_after_decision_as_of"}
                       for episode in verified if episode not in candidates]
            for episode in candidates:
                plans = [self.get_order_plan(plan_id) for plan_id in episode.order_plan_ids]
                orders = [order for plan in plans for order in plan.orders if order.symbol == episode.symbol]
                if orders and all(order.forced for order in orders):
                    details.append({"episode_id": episode.episode_id, "score": {}, "total_score": None,
                                    "selected": False, "exclusion_reason": "hard_risk_only_episode"})
                    continue
                outcome = self.get_outcome_card(episode.outcome_card_id or "")
                intents = [self.get_intent_item(item_id) for item_id in episode.intent_item_ids]
                claim_ids = {claim_id for intent in intents for claim_id in (*intent.supporting_card_ids, *intent.opposing_card_ids)}
                claims = [self.get_claim(claim_id) for claim_id in claim_ids]
                historical_agents = {claim.agent for claim in claims}
                historical_stances = {claim.stance for claim in claims}
                score = {
                    "same_symbol": 8 * int(episode.symbol == symbol),
                    "same_regime": 4 * int(outcome.market_regime == regime),
                    "source_overlap": 2 * len(set(profile.get("agents", ())) & historical_agents),
                    "stance_overlap": 2 * len(set(profile.get("stances", ())) & historical_stances),
                    "same_holding_state": 3 * int(bool(profile.get("held")) == any(item.action in {"hold", "reduce", "exit"} for item in intents)),
                }
                rows.append((episode, score))
            rows.sort(key=lambda row: (sum(row[1].values()), row[0].created_at, row[0].episode_id), reverse=True)
            selected, source_counts = [], {}
            for episode, score in rows:
                exclusion = None
                if source_counts.get(episode.symbol, 0) >= 2:
                    exclusion = "source_symbol_diversity_cap"
                elif len(selected) >= limit_per_symbol:
                    exclusion = "top_k_limit"
                else:
                    selected.append(episode)
                    source_counts[episode.symbol] = source_counts.get(episode.symbol, 0) + 1
                details.append({"episode_id": episode.episode_id, "score": score,
                                "total_score": sum(score.values()), "selected": exclusion is None,
                                "exclusion_reason": exclusion})
            result[symbol] = tuple(selected)
            audit[symbol] = details
        return result, audit

    def materialize_review_context(self, episode: PositionEpisode) -> dict[str, Any]:
        """入 prompt 前沿 ID 物化 review 素材：决策与执行；结果由调用方单独传入。

        Plan.md 3.2.4：Review 通过 Episode 读取关联的 IntentItem、OrderPlan 与 Fill。研究
        证据不进入 Outcome 复盘 prompt；物化只发生在调用侧，账本不保存摘要副本。
        """
        intents = [self.get_intent_item(item_id) for item_id in episode.intent_item_ids]
        plans = [self.get_order_plan(plan_id) for plan_id in episode.order_plan_ids]
        fills = [self.get_fill(fill_id) for fill_id in episode.fill_ids]
        return {
            "original_decisions": [{"action": item.action, "strength": item.strength,
                                    "horizon_days": item.horizon_days, "horizon_action": item.horizon_action,
                                    "rationale": item.rationale} for item in intents],
            "execution": {
                "orders": [{"side": order.side, "target_weight": order.target_weight, "forced": order.forced,
                            "reason_codes": [code.value for code in order.reason_codes]}
                           for plan in plans for order in plan.orders],
                "fills": [{"fill_id": fill.fill_id, "side": fill.side, "status": fill.status,
                           "filled_shares": fill.filled_shares, "executed_price": fill.executed_price,
                           "commission": fill.commission, "reason_codes": [code.value for code in fill.reason_codes],
                           "executed_at": fill.executed_at.isoformat()} for fill in fills],
            },
        }

    def _latest_position_episodes(self) -> tuple[PositionEpisode, ...]:
        with self._connect() as conn:
            rows = tuple(conn.execute("SELECT payload FROM position_episodes ORDER BY created_at, object_id"))
        latest: dict[str, PositionEpisode] = {}
        for row in rows:
            episode = PositionEpisode.model_validate_json(row["payload"])
            if episode.episode_id not in latest or episode.revision > latest[episode.episode_id].revision:
                latest[episode.episode_id] = episode
        return tuple(latest.values())

    def append_outcome_card(self, outcome: OutcomeCard) -> None:
        self._append("outcome_cards", outcome.outcome_card_id, outcome.trace_id,
                     outcome.model_dump(mode="json"), outcome.created_at)

    def get_outcome_card(self, outcome_card_id: str) -> OutcomeCard:
        return OutcomeCard.model_validate_json(self._get("outcome_cards", outcome_card_id)["payload"])

    def list_outcomes_for_order_plan(self, order_plan_id: str) -> tuple[OutcomeCard, ...]:
        outcomes = []
        for episode in self._latest_position_episodes():
            if order_plan_id in episode.order_plan_ids and episode.outcome_card_id:
                outcomes.append(self.get_outcome_card(episode.outcome_card_id))
        return tuple(outcomes)

    def get_outcome_for_episode(self, episode_id: str) -> OutcomeCard | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM outcome_cards WHERE json_extract(payload, '$.episode_id') = ? "
                "ORDER BY created_at DESC LIMIT 1", (episode_id,)
            ).fetchone()
        return OutcomeCard.model_validate_json(row["payload"]) if row else None

    def append_review_label(self, label: ReviewLabel) -> None:
        self._append("review_labels", label.review_label_id, label.trace_id,
                     label.model_dump(mode="json"), label.created_at)

    def get_review_label(self, review_label_id: str) -> ReviewLabel:
        return ReviewLabel.model_validate_json(self._get("review_labels", review_label_id)["payload"])

    def get_verified_review_label(self, outcome_card_id: str) -> ReviewLabel | None:
        """只有完整且通过经验准入的反思可随历史案例进入下一轮 Committee。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM review_labels WHERE json_extract(payload, '$.outcome_card_id') = ? "
                "AND json_extract(payload, '$.status') = 'complete' "
                "AND json_extract(payload, '$.memory_eligibility') = 'eligible' "
                "ORDER BY created_at DESC LIMIT 1",
                (outcome_card_id,),
            ).fetchone()
            return ReviewLabel.model_validate_json(row["payload"]) if row else None

    def list_verified_memory_outcomes(self, *, limit: int = 8) -> tuple[OutcomeCard, ...]:
        """仅返回已通过经验准入的客观结果，供上游按相关性再筛选。"""
        if limit < 1:
            raise ValueError("memory outcome limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT o.payload FROM outcome_cards o JOIN review_labels r "
                "ON json_extract(r.payload, '$.outcome_card_id') = o.object_id "
                "WHERE json_extract(r.payload, '$.status') = 'complete' "
                "AND json_extract(r.payload, '$.memory_eligibility') = 'eligible' "
                "ORDER BY o.created_at DESC LIMIT ?", (limit,),
            )
            return tuple(OutcomeCard.model_validate_json(row["payload"]) for row in rows)

    def select_verified_memory_outcomes(self, *, symbols: tuple[str, ...], regime: str | None,
                                        limit: int = 4) -> tuple[OutcomeCard, ...]:
        """先以标的和同一冻结 regime 缩小案例池；不匹配时宁可返回空而非泛化记忆。"""
        if not symbols or limit < 1:
            raise ValueError("memory selection requires symbols and a positive limit")
        selected = [outcome for outcome in self.list_verified_memory_outcomes(limit=64)
                    if outcome.symbol in set(symbols) and outcome.market_regime == regime]
        return tuple(selected[:limit])

    def _append(self, table: str, object_id: str, trace_id: str, payload: dict[str, Any], created_at: datetime) -> None:
        if table not in OBJECT_TABLES[1:]:
            raise ValueError(f"unsupported ledger table: {table}")
        with self._connect() as conn:
            trace = conn.execute("SELECT status FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            if trace is None or trace["status"] != "running":
                raise ValueError(f"trace is not writable: {trace_id}")
            conn.execute(f"INSERT INTO {table}(object_id, trace_id, payload, created_at) VALUES (?, ?, ?, ?)", (object_id, trace_id, self._dump(payload), created_at.isoformat()))

    def _find_payloads(self, table: str, json_path: str, value: str) -> tuple[sqlite3.Row, ...]:
        if table not in OBJECT_TABLES[1:]:
            raise ValueError(f"unsupported ledger table: {table}")
        with self._connect() as conn:
            return tuple(conn.execute(
                f"SELECT payload FROM {table} WHERE json_extract(payload, ?) = ? ORDER BY created_at, object_id",
                (json_path, value),
            ))

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
