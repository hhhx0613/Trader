"""账本与正文库的只读视图 / Read-only views for the visualization UI.

前端展示与「历史回放」所需的一切查询都集中在这里：只做 SELECT，绝不写账本，
也不给 Agent 开新的数据入口（见 docs/Plan.md 4.4）。逐张对象的完整正文由
`RawPayloadStore` 按 EvidenceCard 的指针解析，指针校验失败即报错，不做静默兜底。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.database.contracts import RawReference
from core.database.raw import RawPayloadStore

# 账本里的对象表 → 前端展示分组名；顺序即前端分组顺序
_TABLES = (
    ("snapshots", "snapshot"),
    ("evidence_cards", "evidence_card"),
    ("claim_cards", "claim_card"),
    ("research_packets", "research_packet"),
    ("thesis_books", "thesis_book"),
    ("portfolio_intents", "portfolio_intent"),
    ("intent_constraints", "intent_constraints"),
    ("target_portfolios", "target_portfolio"),
    ("risk_projected_portfolios", "risk_projected_portfolio"),
    ("order_plans", "order_plan"),
    # 阶段 5-6：执行与复盘后半程，回放页据此把 episode→outcome→review→引用画完整
    ("fills", "fill"),
    ("position_episodes", "position_episode"),
    ("outcome_cards", "outcome_card"),
    ("review_labels", "review_label"),
)

_OBJECT_KEY = {
    "snapshots": "snapshot_id",
    "evidence_cards": "evidence_id",
    "claim_cards": "claim_id",
    "research_packets": "packet_id",
    "thesis_books": "thesis_book_id",
    "portfolio_intents": "intent_id",
    "intent_constraints": "intent_id",
    "target_portfolios": "target_portfolio_id",
    "risk_projected_portfolios": "projected_portfolio_id",
    "order_plans": "order_plan_id",
    "fills": "fill_id",
    # position_episodes 行主键是 {episode_id}:{revision}；对外身份用 payload 里的 episode_id，详情取最新 revision
    "position_episodes": "episode_id",
    "outcome_cards": "outcome_card_id",
    "review_labels": "review_label_id",
}


def _digest_label(kind: str, p: dict[str, Any]) -> dict[str, Any]:
    """一行可进 chip 的摘要：只取能定性的一句话，不取大段正文（全文另走 /api/raw）。"""
    sym = p.get("symbol") or ""
    if kind == "evidence_card":
        # 卡片本身不存正文（title/summary 在 raw 里）：chip 摘要只用卡片自带的 source·kind，
        # 全文仍由抽屉按 evidence_id 走 /api/raw 取。
        text = p.get("title") or p.get("summary") or f"{p.get('source') or ''} · {p.get('kind') or ''}"
        return {"kind": kind, "symbol": sym, "source": p.get("source"), "label": text[:140]}
    if kind == "claim_card":
        return {"kind": kind, "symbol": sym, "stance": p.get("stance"),
                "label": (p.get("statement") or "")[:140]}
    if kind == "position_episode":
        return {"kind": kind, "symbol": sym, "label": f"{p.get('status') or ''}"}
    if kind == "outcome_card":
        net = p.get("net_return")
        return {"kind": kind, "symbol": sym, "label": p.get("observation") or (f"net={net}" if net is not None else "")}
    if kind == "review_label":
        return {"kind": kind, "symbol": sym, "label": f"{p.get('status') or ''} · {p.get('memory_eligibility') or ''}"}
    if kind == "fill":
        return {"kind": kind, "symbol": sym, "label": f"{p.get('side') or ''} {p.get('filled_shares')}/{p.get('requested_shares')}"}
    if kind == "order_plan":
        return {"kind": kind, "symbol": sym, "label": f"{len(p.get('orders') or [])} orders"}
    if kind == "portfolio_intent":
        # 委员会决策的实质内容都在 items 里：理由全文展示，不截断（用户明确要求）
        parts = []
        for i in p.get("items") or []:
            seg = f"{i.get('symbol')}:{i.get('action')}"
            if i.get("horizon_days"):
                seg += f"/{i['horizon_days']}d"
            seg += f"(强度{i.get('strength')},P{i.get('priority')})"
            rationale = " ".join(str(i.get("rationale") or "").split())
            if rationale:
                seg += f" {rationale}"
            parts.append(seg)
        return {"kind": kind, "symbol": sym, "label": " | ".join(parts)}
    return {"kind": kind, "symbol": sym, "label": ""}


class LedgerReader:
    """按对象表聚合的只读查询器；每次调用开一条新连接，用完即关。"""

    def __init__(self, ledger_path: str | Path, raw_dir: str | Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.raw_dir = Path(raw_dir)

    def _connect(self) -> sqlite3.Connection | None:
        if not self.ledger_path.exists():
            return None
        conn = sqlite3.connect(self.ledger_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _payload(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["payload"])

    # ---------- Snapshot ----------

    def snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        """最近的冻结 Snapshot，供「复用已有 Snapshot」下拉与回放入口。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute("SELECT object_id, trace_id, payload, created_at FROM snapshots "
                                "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            payload = self._payload(row)
            out.append({"snapshot_id": payload["snapshot_id"], "trace_id": row["trace_id"],
                        "as_of": payload.get("as_of"), "symbols": payload.get("symbols"),
                        "evidence": len(payload.get("evidence_ids") or []),
                        "source_versions": payload.get("source_versions"),
                        "created_at": row["created_at"]})
        return out

    def snapshot_detail(self, snapshot_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT object_id, trace_id, payload, created_at FROM snapshots "
                               "WHERE object_id=?", (snapshot_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        payload = self._payload(row)
        cards = self._cards_of_ids(payload.get("evidence_ids") or [], snapshot_id)
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for card in cards:
            grouped.setdefault(card["symbol"], {}).setdefault(card["kind"], []).append(card)
        return {"snapshot": payload, "trace_id": row["trace_id"], "created_at": row["created_at"],
                "evidence_by_symbol": grouped}

    def _cards_of_ids(self, evidence_ids: list[str], snapshot_id: str) -> list[dict[str, Any]]:
        """一次查询取回卡片摘要（不含正文），缺失 ID 不静默跳过。"""
        if not evidence_ids:
            return []
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT e.object_id, e.payload FROM evidence_cards e, json_each(?) AS ids "
                "WHERE e.object_id = ids.value "
                "ORDER BY datetime(json_extract(e.payload,'$.available_at')) DESC",
                (json.dumps(list(evidence_ids)),),
            ).fetchall()
            missing = conn.execute("SELECT count(*) FROM json_each(?) AS ids "
                                   "LEFT JOIN evidence_cards e ON e.object_id = ids.value "
                                   "WHERE e.object_id IS NULL", (json.dumps(list(evidence_ids)),)).fetchone()[0]
        finally:
            conn.close()
        cards = []
        for row in rows:
            payload = self._payload(row)
            cards.append({"evidence_id": payload["evidence_id"], "kind": payload.get("kind"),
                          "symbol": payload.get("symbol"), "source": payload.get("source"),
                          "published_at": payload.get("published_at"), "available_at": payload.get("available_at"),
                          "content_hash": payload.get("content_hash"), "canonical_url": payload.get("canonical_url"),
                          "raw": payload.get("raw"), "missing": False})
        if missing:
            cards.append({"evidence_id": f"missing:{missing}", "kind": "unknown", "symbol": "",
                          "source": f"{snapshot_id} 引用了 {missing} 张不在账本的卡", "missing": True})
        return cards

    # ---------- 批量摘要（前端 chip 不再只显裸 ID） ----------

    def digests(self, refs: list[str]) -> dict[str, dict[str, Any]]:
        """把一批「kind:id」一次解析成一行摘要：只查摘要字段、不取正文。

        未知 kind 或缺失 id 不报错、不兜底：前端拿不到的 id 自然回落短 ID。
        """
        by_kind: dict[str, set[str]] = {}
        for ref in refs:
            kind, _, oid = str(ref).partition(":")
            if kind and oid:
                by_kind.setdefault(kind, set()).add(oid)
        out: dict[str, dict[str, Any]] = {}
        conn = self._connect()
        if conn is None:
            return out
        try:
            for kind, ids in by_kind.items():
                table = next((t for t, k in _TABLES if k == kind), None)
                if table is None:
                    continue
                key = _OBJECT_KEY[table]
                # position_episodes 行主键带 revision，对外身份是 payload.episode_id，只能按 payload 点查
                match = ("json_extract(t.payload,'$.episode_id') = ids.value"
                         if kind == "position_episode" else "t.object_id = ids.value")
                rows = conn.execute(
                    f"SELECT t.payload FROM {table} AS t, json_each(?) AS ids WHERE {match}",
                    (json.dumps(list(ids)),)).fetchall()
                for row in rows:
                    payload = self._payload(row)
                    object_id = payload.get(key)
                    if object_id in ids:
                        out[str(object_id)] = _digest_label(kind, payload)
        finally:
            conn.close()
        return out

    # ---------- Trace ----------

    def traces(self, limit: int = 40) -> list[dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute("SELECT trace_id, status, created_at, completed_at FROM traces "
                                "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
            counts = {}
            for table, _kind in _TABLES:
                for row in conn.execute(f"SELECT trace_id, count(*) AS n FROM {table} GROUP BY trace_id"):
                    counts.setdefault(row["trace_id"], {})[table] = row["n"]
            tools = {row["trace_id"]: row["n"] for row in
                     conn.execute("SELECT trace_id, count(*) AS n FROM tool_calls GROUP BY trace_id")}
        finally:
            conn.close()
        out = []
        for row in rows:
            summary = counts.get(row["trace_id"], {})
            out.append({"trace_id": row["trace_id"], "status": row["status"], "created_at": row["created_at"],
                        "completed_at": row["completed_at"], "objects": summary,
                        "tool_calls": tools.get(row["trace_id"], 0),
                        "kinds": {kind: summary.get(table, 0) for table, kind in _TABLES}})
        return out

    def trace_detail(self, trace_id: str) -> dict[str, Any]:
        """一次 trace 的全部对象 + 工具/查询审计，供「账本回放」整幅展示。

        证据卡逐轮累积，单 trace 可能上千张；回放页只需要引用链，故最多外发前 500 张，
        完整名单由 Snapshot 详情按需取。
        """
        conn = self._connect()
        if conn is None:
            return {"trace_id": trace_id, "objects": {}, "tool_calls": [], "queries": []}
        try:
            objects: dict[str, list[dict[str, Any]]] = {}
            for table, kind in _TABLES:
                limit = " LIMIT 500" if table == "evidence_cards" else ""
                rows = conn.execute(f"SELECT payload FROM {table} WHERE trace_id=? ORDER BY created_at" + limit,
                                    (trace_id,)).fetchall()
                objects[kind] = [self._payload(row) for row in rows]
            tool_rows = conn.execute("SELECT * FROM tool_calls WHERE trace_id=? ORDER BY call_id",
                                     (trace_id,)).fetchall()
            query_rows = conn.execute("SELECT * FROM gateway_queries WHERE trace_id=? ORDER BY query_id",
                                      (trace_id,)).fetchall()
        finally:
            conn.close()
        return {
            "trace_id": trace_id,
            "objects": objects,
            "tool_calls": [{"node": r["node"], "symbol": r["symbol"], "tool": r["tool_name"],
                            "requested_at": r["requested_at"], "result_count": r["result_count"],
                            "arguments": json.loads(r["arguments"]),
                            "evidence_ids": json.loads(r["evidence_ids"])} for r in tool_rows],
            "queries": [{"symbol": r["symbol"], "kind": r["query_kind"], "requested_at": r["requested_at"],
                         "result_count": r["result_count"]} for r in query_rows],
        }

    def thesis_book_for_trace(self, trace_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT payload FROM thesis_books WHERE trace_id=? ORDER BY created_at",
                               (trace_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else self._payload(row)

    # ---------- 单对象详情（含引用链，前端点 ID 即溯源） ----------

    def object_detail(self, kind: str, object_id: str) -> dict[str, Any] | None:
        table = next((t for t, k in _TABLES if k == kind), None)
        if table is None:
            raise ValueError(f"unknown object kind: {kind}")
        conn = self._connect()
        if conn is None:
            return None
        try:
            if kind == "position_episode":
                # Episode 按 revision 追加不覆盖；详情只暴露最新快照，历史 revision 留在账本
                row = conn.execute(f"SELECT trace_id, created_at, payload FROM {table} "
                                   "WHERE json_extract(payload, '$.episode_id') = ? "
                                   "ORDER BY CAST(json_extract(payload, '$.revision') AS INTEGER) DESC LIMIT 1",
                                   (object_id,)).fetchone()
            else:
                row = conn.execute(f"SELECT trace_id, created_at, payload FROM {table} WHERE object_id=?",
                                   (object_id,)).fetchone()
            if row is None:
                return None
            payload = self._payload(row)
            links: dict[str, Any] = {}
            if kind == "evidence_card":
                links["cited_by_claims"] = [self._payload(c) for c in conn.execute(
                    "SELECT c.payload FROM claim_cards c, json_each(json_extract(c.payload,'$.supporting_evidence_ids')) AS e "
                    "WHERE e.value = ?", (object_id,)).fetchall()]
                links["contradicted_by_claims"] = [self._payload(c) for c in conn.execute(
                    "SELECT c.payload FROM claim_cards c, "
                    "json_each(json_extract(c.payload,'$.contradicting_evidence_ids')) AS e WHERE e.value = ?",
                    (object_id,)).fetchall()]
            elif kind == "claim_card":
                links["evidence"] = self._evidence_of(payload)
                links["thesis_book_ids"] = [self._payload(t)["thesis_book_id"] for t in conn.execute(
                    "SELECT t.payload FROM thesis_books t, json_each(json_extract(t.payload,'$.packet_ids')) AS p, "
                    "research_packets r WHERE r.object_id = p.value AND "
                    "EXISTS (SELECT 1 FROM json_each(json_extract(r.payload,'$.claim_card_ids')) AS c "
                    "WHERE c.value = ?)", (object_id,)).fetchall()]
            elif kind in ("research_packet", "snapshot", "thesis_book", "portfolio_intent"):
                if kind == "research_packet":
                    links["claims"] = [self._payload(c) for c in conn.execute(
                        "SELECT p.payload FROM claim_cards p, "
                        "json_each(json_extract(?, '$.claim_card_ids')) AS i WHERE p.object_id = i.value",
                        (json.dumps(payload),)).fetchall()]
                    links["evidence"] = self._evidence_of_many(links["claims"])
                if kind == "snapshot":
                    links["evidence"] = self._cards_of_ids(payload.get("evidence_ids") or [], object_id)
                if kind in ("thesis_book", "portfolio_intent"):
                    ids = payload.get("packet_ids") or []
                    links["packets"] = [self._payload(p) for p in conn.execute(
                        "SELECT r.payload FROM research_packets r, json_each(?) AS i WHERE r.object_id = i.value",
                        (json.dumps(list(ids)),)).fetchall()] if ids else []
                if kind == "portfolio_intent":
                    # 校准引用是「记忆→委员会」这条线的落点：把本轮意图引用的历史案例物化出来
                    cited = sorted({episode_id for item in (payload.get("items") or ())
                                    for episode_id in (item.get("calibration_episode_ids") or ())})
                    links["calibration_episodes"] = [self._latest_episode(conn, episode_id)[0]
                                                     for episode_id in cited
                                                     if self._latest_episode(conn, episode_id)]
            elif kind == "position_episode":
                links["outcome_card"] = self._rows_by_object_ids(conn, "outcome_cards", [payload.get("outcome_card_id")])
                links["review_label"] = self._rows_by_object_ids(conn, "review_labels", [payload.get("review_label_id")])
                links["fills"] = self._rows_by_object_ids(conn, "fills", list(payload.get("fill_ids") or ()))
                links["intents_citing"] = self._intents_citing(conn, payload.get("episode_id"))
            elif kind == "outcome_card":
                links["fills"] = self._rows_by_object_ids(conn, "fills", list(payload.get("fill_ids") or ()))
                links["episode"] = self._latest_episode(conn, payload.get("episode_id"))
            elif kind == "review_label":
                links["outcome_card"] = self._rows_by_object_ids(conn, "outcome_cards", [payload.get("outcome_card_id")])
            elif kind == "fill":
                links["order_plan"] = self._rows_by_object_ids(conn, "order_plans", [payload.get("order_plan_id")])
        finally:
            conn.close()
        return {"kind": kind, "id": payload[_OBJECT_KEY[table]], "trace_id": row["trace_id"],
                "created_at": row["created_at"], "data": payload, "links": links}

    @staticmethod
    def _rows_by_object_ids(conn: sqlite3.Connection, table: str, object_ids: list[Any]) -> list[dict[str, Any]]:
        """fills/outcome_cards/review_labels/order_plans 的行主键即其对外 ID，可直接点查。"""
        out = []
        for object_id in object_ids:
            if not object_id:
                continue
            row = conn.execute(f"SELECT payload FROM {table} WHERE object_id=?", (str(object_id),)).fetchone()
            if row is not None:
                out.append(json.loads(row["payload"]))
        return out

    @staticmethod
    def _latest_episode(conn: sqlite3.Connection, episode_id: Any) -> list[dict[str, Any]]:
        if not episode_id:
            return []
        row = conn.execute("SELECT payload FROM position_episodes "
                           "WHERE json_extract(payload,'$.episode_id') = ? "
                           "ORDER BY CAST(json_extract(payload,'$.revision') AS INTEGER) DESC LIMIT 1",
                           (str(episode_id),)).fetchone()
        return [json.loads(row["payload"])] if row else []

    @staticmethod
    def _intents_citing(conn: sqlite3.Connection, episode_id: Any) -> list[dict[str, Any]]:
        """反查哪些 PortfolioIntent 在 calibration_episode_ids 里引用过该案例。"""
        if not episode_id:
            return []
        rows = conn.execute(
            "SELECT p.payload FROM portfolio_intents p, json_each(json_extract(p.payload,'$.items')) AS it "
            "WHERE EXISTS (SELECT 1 FROM json_each(json_extract(it.value,'$.calibration_episode_ids')) AS ce "
            "WHERE ce.value = ?)", (str(episode_id),)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def raw_payload(self, card: dict[str, Any]) -> dict[str, Any] | None:
        """按 EvidenceCard 的指针取正文；哈希不符时让 RawPayloadStore 直接报错。"""
        reference = card.get("raw")
        if not isinstance(reference, dict):
            return None
        store = RawPayloadStore(self.raw_dir)
        try:
            return store.get(RawReference.model_validate(reference))
        except (FileNotFoundError, ValueError):
            return None
        finally:
            store.close()

    def _evidence_of(self, claim: dict[str, Any]) -> list[dict[str, Any]]:
        ids = list(claim.get("supporting_evidence_ids") or ()) + list(claim.get("contradicting_evidence_ids") or ())
        return self._cards_of_ids(sorted(set(ids)), "")

    def _evidence_of_many(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids: set[str] = set()
        for claim in claims:
            ids.update(claim.get("supporting_evidence_ids") or ())
            ids.update(claim.get("contradicting_evidence_ids") or ())
        return self._cards_of_ids(sorted(ids), "")

    # ---------- 历史运行（观测产物，不在账本里） ----------

    def run_index(self, runs_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
        from viz.server import load_run_events  # 局部导入：避免 server ↔ readmodel 循环依赖

        directory = Path(runs_dir)
        if not directory.exists():
            return []
        runs = []
        for path in sorted(directory.glob("viz_*.jsonl"), reverse=True)[:limit]:
            try:
                meta, events = load_run_events(path)
            except OSError:
                continue
            end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
            runs.append({"run_id": meta.get("run_id", path.stem), "created_at": meta.get("created_at"),
                         "state": (end or {}).get("state", "running"), "error": (end or {}).get("error"),
                         "duration_s": (end or {}).get("duration_s"), "events": len(events),
                         "options": meta.get("options"), "file": path.name,
                         "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
        return runs
