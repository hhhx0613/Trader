"""Snapshot 的只读审计入口 / Read-only, audited access to frozen snapshots.

未来 Agent 只能通过此入口读取冻结事实。每次读取必须精确匹配 snapshot_id、
symbol 与 as_of，并在账本写入查询审计记录；该模块不暴露 Provider、文件系统
或数据库句柄给 Agent。

Future agents may read frozen facts only through this gateway. Every read must
match snapshot_id, symbol, and as_of exactly and is recorded in the ledger. The
module never exposes providers, filesystem paths, or database handles to agents.

Gateway 不发起任何网络请求，也不建卡；拉数据与建卡是采集脚本在冻结之前做的事。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from typing import Any, Iterable

from .contracts import EvidenceCard, ResearchSnapshot
from .raw import RawPayloadStore
from .store import ResearchLedger

# 补查返回上限：冻结集内一个高频词（如 "nvda"）可命中整批新闻，必须按相关度截断，
# 否则 search_evidence 会把全部证据洪水式倒回 prompt，失去"定向 narrowing"的意义。
_DEFAULT_SEARCH_RESULTS = 8
_MAX_SEARCH_RESULTS = 20
# 最低相似度（查询词在正文里的覆盖比例）：低于此阈值视为不相关，宁可空返回也不噪声填充。
_MIN_SIMILARITY = 0.5


class DataGateway:
    """执行 Snapshot 边界检查与读取审计 / Enforce Snapshot boundaries and audit reads."""
    def __init__(self, ledger: ResearchLedger, raw_store: RawPayloadStore | None = None) -> None:
        self.ledger = ledger
        # Gateway 可以在边界内解析正文；调用方永远拿不到 RawPayloadStore 或其路径。
        self._raw_store = raw_store

    def evidence_for_symbol(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> list[EvidenceCard]:
        """返回该 Snapshot 在该标的上的冻结卡；本方法不开网络、不建卡、不补窗口。"""
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

    def kind_counts(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, int]:
        """统计该标的在冻结 Snapshot 内各证据类别的可用卡数。

        Agent 据此预判空类别：与其让模型把某个不存在的 kind 反复换参数枚举，不如在读取
        层直接给出事实目录，令空结果可解释。
        """
        cards = self.evidence_for_symbol(snapshot_id=snapshot_id, symbol=symbol, as_of=as_of)
        counts = {"news": 0, "filing": 0, "market": 0}
        for card in cards:
            if card.kind in counts:
                counts[card.kind] += 1
        return counts

    def search_evidence(self, *, snapshot_id: str, symbol: str, as_of: datetime,
                        query: str, kinds: Iterable[str] | None = None,
                        k: int = _DEFAULT_SEARCH_RESULTS,
                        min_score: float = _MIN_SIMILARITY) -> list[dict[str, Any]]:
        """受控全文补查 / Rank frozen evidence by similarity, return the top-k.

        搜索只在已经冻结的卡片正文中进行，不能把 Agent 的查询变成对 Provider 或网络的
        回退访问；返回值保留 EvidenceCard ID，供后续 ClaimCard 精确引用。

        相似度 = 查询词在正文中的覆盖比例（命中词数 / 查询词数）：模型写的是关键词袋
        （如 "NVDA market trend volatility"）而非连续短语，覆盖度能天然地按“命中了多少
        概念”排序。低于 min_score 丢弃，再按相似度降序、同分按时间倒序截断到前 k 条——
        这样搜单个高频词（如 "nvda"）只返回最相关的几条，而不是把整批新闻全部倒回。
        """
        allowed = set(kinds or ("news", "filing", "market"))
        if not allowed <= {"news", "filing", "market"}:
            raise ValueError("unsupported evidence kind")
        limit = max(1, min(int(k), _MAX_SEARCH_RESULTS))
        terms = {token for token in re.split(r"\W+", query.strip().lower()) if token}
        if not terms:
            return self._record_result(snapshot_id, symbol, "search_evidence", [])
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self._records(snapshot_id, symbol, as_of, allowed):
            text = item["text"].lower()
            score = sum(1 for token in terms if token in text) / len(terms)
            if score < min_score:
                continue
            scored.append((score, item))
        # 相似度优先，同分按 available_at 倒序，保证确定性可复现
        scored.sort(key=lambda entry: (entry[0], entry[1]["available_at"]), reverse=True)
        results = [item for _score, item in scored[:limit]]
        return self._record_result(snapshot_id, symbol, "search_evidence", results)

    def get_news_batch(self, *, snapshot_id: str, symbol: str, as_of: datetime,
                       max_items: int = 100, max_characters: int = 24_000) -> list[dict[str, Any]]:
        """返回按情绪强度排序的冻结新闻摘录，模型一次看到最有信息量的条目。"""
        if max_items < 1 or max_characters < 1:
            raise ValueError("news batch budgets must be positive")
        records = self._records(snapshot_id, symbol, as_of, {"news"})
        selected: list[dict[str, Any]] = []
        used = 0
        # 按情绪绝对值降序（最有信号力的排前面），同分则按时间倒序
        for item in sorted(records, key=lambda v: (abs(v.get("sentiment_score", 0)), v["available_at"]), reverse=True):
            if len(selected) >= max_items or used + len(item["text"]) > max_characters:
                break
            selected.append(item)
            used += len(item["text"])
        return self._record_result(snapshot_id, symbol, "news_batch", selected)

    def get_filing_section(self, *, snapshot_id: str, symbol: str, as_of: datetime,
                           section: str) -> list[dict[str, Any]]:
        """按关键词返回冻结披露片段；没有匹配不能伪造为完整 SEC 正文。"""
        needle = section.strip().lower()
        items = [item for item in self._records(snapshot_id, symbol, as_of, {"filing"})
                 if not needle or needle in item["text"].lower()]
        return self._record_result(snapshot_id, symbol, "filing_section", items)

    def get_xbrl_facts(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> list[dict[str, Any]]:
        """返回已冻结的 XBRL 观测；只识别采集器明确标记的 XBRL 来源。"""
        items = [item for item in self._records(snapshot_id, symbol, as_of, {"filing"})
                 if "xbrl" in item["source"].lower()]
        return self._record_result(snapshot_id, symbol, "xbrl_facts", items)

    def get_market_slice(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> list[dict[str, Any]]:
        """返回冻结行情特征或 K 线批次，数值仍由采集阶段程序计算。"""
        return self._record_result(snapshot_id, symbol, "market_slice",
                                   self._records(snapshot_id, symbol, as_of, {"market"}))

    def get_peer_comparison(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, list[dict[str, Any]]]:
        """以同一 Snapshot 股票池作为可审计 peers，不能临时扩展股票池。"""
        snapshot = self._validated_snapshot(snapshot_id, symbol, as_of)
        result = {peer: self._records(snapshot_id, peer, as_of, {"market"})
                  for peer in snapshot.symbols if peer != symbol.strip().upper()}
        self._audit(snapshot, symbol, "peer_comparison", sum(len(items) for items in result.values()))
        return result

    def get_regime(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, Any] | None:
        """只返回冻结账户状态中已声明的 regime；本阶段不推断或联网补齐宏观状态。"""
        snapshot = self._validated_snapshot(snapshot_id, symbol, as_of)
        value = snapshot.account_state.get("regime")
        self._audit(snapshot, symbol, "regime", int(value is not None))
        return value if isinstance(value, dict) else ({"label": value} if value is not None else None)

    def get_current_portfolio(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, Any]:
        """组合输入来自 Snapshot，不允许 Agent 越过冻结边界读取实时账户。"""
        snapshot = self._validated_snapshot(snapshot_id, symbol, as_of)
        self._audit(snapshot, symbol, "current_portfolio", 1)
        return dict(snapshot.account_state)

    def estimate_turnover_cost(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, Any] | None:
        """仅回传采集时冻结的成本估计；成本模型将在后续确定性阶段实现。"""
        portfolio = self.get_current_portfolio(snapshot_id=snapshot_id, symbol=symbol, as_of=as_of)
        value = portfolio.get("turnover_cost_estimate")
        snapshot = self._validated_snapshot(snapshot_id, symbol, as_of)
        self._audit(snapshot, symbol, "turnover_cost", int(value is not None))
        return value if isinstance(value, dict) else ({"value": value} if value is not None else None)

    def snapshot_metadata(self, *, snapshot_id: str, symbol: str, as_of: datetime) -> dict[str, Any]:
        """图编排所需的最小冻结上下文，不泄露账本或 Provider 句柄。"""
        snapshot = self._validated_snapshot(snapshot_id, symbol, as_of)
        self._audit(snapshot, symbol, "snapshot_metadata", 1)
        return {"snapshot_id": snapshot.snapshot_id, "trace_id": snapshot.trace_id,
                "as_of": snapshot.as_of, "symbol": symbol.strip().upper()}

    def _validated_snapshot(self, snapshot_id: str, symbol: str, as_of: datetime) -> ResearchSnapshot:
        snapshot = self.ledger.get_snapshot(snapshot_id)
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol not in snapshot.symbols:
            raise ValueError(f"symbol is not frozen in snapshot: {normalized_symbol}")
        if as_of != snapshot.as_of:
            raise ValueError("gateway as_of must exactly match frozen snapshot")
        return snapshot

    def _records(self, snapshot_id: str, symbol: str, as_of: datetime, kinds: set[str]) -> list[dict[str, Any]]:
        cards = self.evidence_for_symbol(snapshot_id=snapshot_id, symbol=symbol, as_of=as_of)
        if self._raw_store is None:
            raise RuntimeError("gateway raw payload access is not configured")
        return self._records_from_cards(card for card in cards if card.kind in kinds)

    def _records_from_cards(self, cards: Iterable[EvidenceCard]) -> list[dict[str, Any]]:
        if self._raw_store is None:
            raise RuntimeError("gateway raw payload access is not configured")
        records = []
        for card in cards:
            payload = self._raw_store.get(card.raw)
            text = "\n".join(str(payload.get(key, "")) for key in ("title", "summary", "body"))
            # published_at/available_at 统一序列化为 ISO 字符串：调用方（risk_critic 的 peer/portfolio 上下文、
            # 各工具 json.dumps）不得拿到裸 datetime——否则 str(prompt) 会混进 datetime.datetime(...) 这类
            # 函数调用表达式，破坏「一律 JSON 模式」口径并让观测层的 ast.literal_eval 回解失败。
            # 同一 UTC 偏移下 ISO 串按字典序即时序，下方按 available_at 排序的语义保持不变。
            rec = {"evidence_id": card.evidence_id, "kind": card.kind, "source": card.source,
                   "published_at": card.published_at.isoformat(), "available_at": card.available_at.isoformat(),
                   "canonical_url": card.canonical_url, "title": payload.get("title", ""),
                   "summary": payload.get("summary", ""), "text": text}
            if "sentiment_score" in payload:
                rec["sentiment_score"] = payload["sentiment_score"]
            records.append(rec)
        return records

    def _record_result(self, snapshot_id: str, symbol: str, query_kind: str,
                       results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot = self.ledger.get_snapshot(snapshot_id)
        self._audit(snapshot, symbol, query_kind, len(results))
        return results

    def _audit(self, snapshot: ResearchSnapshot, symbol: str, query_kind: str, count: int) -> None:
        self.ledger.record_query(trace_id=snapshot.trace_id, snapshot_id=snapshot.snapshot_id,
                                 symbol=symbol.strip().upper(), query_kind=query_kind,
                                 result_count=count, requested_at=datetime.now(timezone.utc))
