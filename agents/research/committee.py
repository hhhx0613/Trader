"""跨标的 Committee / Frozen ThesisBook to cited PortfolioIntent only."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from core.research import DataGateway, ResearchLedger
from core.research.contracts import IntentItem, PortfolioIntent, ResearchPacket, ThesisBook

from .types import JsonModel
from .prompting import load_prompt


class Committee:
    """只在 ThesisBook 摘要上形成研究意图，不读取原文或生成仓位、订单。"""

    def __init__(self, *, ledger: ResearchLedger, gateway: DataGateway, model: JsonModel) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.model = model
        self.prompt, self.prompt_version = load_prompt("committee")

    def run(self, *, snapshot_id: str, packet_ids: tuple[str, ...], as_of: datetime,
            trace_id: str | None = None) -> PortfolioIntent:
        """在独立 trace 中持久化 ThesisBook 和全标的 Intent，避免改写上游研究。"""
        packets = self._packets(snapshot_id, packet_ids)
        trace_id = trace_id or f"committee_{uuid4().hex}"
        self.ledger.start_trace(trace_id, as_of)
        try:
            book = self._build_book(trace_id, snapshot_id, packets, as_of)
            self.ledger.append_thesis_book(book)
            intent = self._decide(trace_id, book, packets, as_of)
            self.ledger.append_intent(intent)
            self.ledger.complete_trace(trace_id, as_of)
            return intent
        except Exception:
            # 中断 trace 保留为 running，不能伪造一份已完成但不可重放的 Committee 结论。
            raise

    def _packets(self, snapshot_id: str, packet_ids: tuple[str, ...]) -> tuple[ResearchPacket, ...]:
        if not packet_ids or len(set(packet_ids)) != len(packet_ids):
            raise ValueError("committee requires unique packet IDs")
        packets = tuple(self.ledger.get_packet(packet_id) for packet_id in packet_ids)
        if any(packet.snapshot_id != snapshot_id for packet in packets):
            raise ValueError("committee packets must belong to the requested snapshot")
        symbols = [packet.symbol for packet in packets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("committee requires one packet per symbol")
        return packets

    def _build_book(self, trace_id: str, snapshot_id: str, packets: tuple[ResearchPacket, ...],
                    as_of: datetime) -> ThesisBook:
        # 组合状态和 regime 均从同一 Snapshot 读取；此处不允许临时访问实时账户。
        anchor = packets[0].symbol
        portfolio = self.gateway.get_current_portfolio(snapshot_id=snapshot_id, symbol=anchor, as_of=as_of)
        regime = self.gateway.get_regime(snapshot_id=snapshot_id, symbol=anchor, as_of=as_of)
        return ThesisBook(trace_id=trace_id, snapshot_id=snapshot_id,
                          packet_ids=tuple(packet.packet_id for packet in packets),
                          portfolio_state=portfolio, regime=(regime or {}).get("label"))

    def _decide(self, trace_id: str, book: ThesisBook, packets: tuple[ResearchPacket, ...],
                as_of: datetime) -> PortfolioIntent:
        packet_by_symbol = {packet.symbol: packet for packet in packets}
        prompt = {"prompt_version": self.prompt_version, "thesis_book": book.model_dump(mode="json"),
                  "packets": [self._packet_digest(packet) for packet in packets],
                  "rule": "Return one intent item per symbol. No weights, returns, prices, or orders."}
        try:
            schema = (self.prompt + '\n\n' + 'Return exactly one JSON object with key items. items must contain exactly one object per '
                      'supplied symbol, with keys symbol, action (long, hold, reduce, exit, abstain), strength '
                      '(integer 1..3), priority (integer >=1), horizon_days (integer or null), '
                      'supporting_card_ids, opposing_card_ids, rationale, and no_trade_reason (string or null). '
                      'Cite only supplied claim_id values. Do not add keys, weights, returns, prices, or orders.')
            response = self.model.chat_json(str(prompt), system_prompt=schema, temperature=0.0,
                                            max_tokens=8000, max_retries=3)
            items = tuple(IntentItem.model_validate(item) for item in response["items"])
        except Exception as exc:
            # 模型调用或输出解析失败：fail-closed 全标的观望，但标为模型故障，不伪装成"模型主动观望"。
            return self._abstain_intent(trace_id, book, packets, f"committee model failure: {type(exc).__name__}")
        # 全局结构违规（集合不齐/重复）说明整份响应的生成基础失效，不部分采信；
        # 标的局部违规只矫正该标的，其余合规意图保留，避免单只越界株连全池信号。
        structural = self._structural_violation(items, packet_by_symbol)
        if structural is not None:
            self._record_correction(trace_id, book, as_of, items, response, [structural])
            return self._abstain_intent(trace_id, book, packets, f"committee rejected invalid output: {structural}")
        violations: list[str] = []
        corrected: list[IntentItem] = []
        for item in items:
            reason = self._item_violation(item, packet_by_symbol[item.symbol])
            if reason is None:
                corrected.append(item)
            else:
                violations.append(f"{item.symbol}: {reason}")
                corrected.append(self._abstain_item(packet_by_symbol[item.symbol], f"corrected violation: {reason}"))
        if violations:
            # 反静默篡改：被矫正的原始响应入账，重放时能分清"模型想说什么"和"系统采纳了什么"。
            self._record_correction(trace_id, book, as_of, items, response, violations)
        return PortfolioIntent(trace_id=trace_id, snapshot_id=book.snapshot_id, items=tuple(corrected))

    @staticmethod
    def _structural_violation(items: tuple[IntentItem, ...],
                              packet_by_symbol: dict[str, ResearchPacket]) -> str | None:
        """全批层面的生成基础校验：不合格时没有任何 item 值得单独信任。"""
        symbols = [item.symbol for item in items]
        if len(set(symbols)) != len(symbols):
            return "committee returned duplicate symbols"
        if set(symbols) != set(packet_by_symbol):
            return "committee must return exactly one item for every packet symbol"
        return None

    @staticmethod
    def _item_violation(item: IntentItem, packet: ResearchPacket) -> str | None:
        """单标的边界校验：违规理由即矫正记录，不回抛以免株连全池。"""
        cited = set(item.supporting_card_ids) | set(item.opposing_card_ids)
        if not cited <= set(packet.claim_card_ids):
            return "cited a claim outside its packet"
        if packet.critic.verdict in {"abstain", "human_review"} and item.action != "abstain":
            return "cannot bypass an abstaining critic"
        if item.action != "abstain" and not item.supporting_card_ids:
            return "non-abstain intent requires a supporting ClaimCard"
        return None

    def _record_correction(self, trace_id: str, book: ThesisBook, as_of: datetime,
                           items: tuple[IntentItem, ...], response: Any,
                           violations: list[str]) -> None:
        # 矫正记录尽力入账：审计不能反噬决策，写失败只降级日志，不改变 abstain 结果。
        try:
            self.ledger.record_tool_call(
                trace_id=trace_id, snapshot_id=book.snapshot_id, symbol=",".join(sorted({item.symbol for item in items})),
                node="committee", tool_name="raw_response",
                arguments={"response": response, "violations": violations},
                result_count=len(violations), evidence_ids=sorted({v.split(":")[0] for v in violations if ":" in v}),
                requested_at=as_of)
        except Exception:
            logging.getLogger(__name__).warning("committee correction record failed: %s", trace_id)

    def _abstain_intent(self, trace_id: str, book: ThesisBook, packets: tuple[ResearchPacket, ...],
                        reason: str) -> PortfolioIntent:
        return PortfolioIntent(trace_id=trace_id, snapshot_id=book.snapshot_id,
                               items=tuple(self._abstain_item(packet, reason) for packet in packets))

    def _packet_digest(self, packet: ResearchPacket) -> dict[str, Any]:
        claims = [self.ledger.get_claim(claim_id) for claim_id in packet.claim_card_ids]
        return {"symbol": packet.symbol, "critic": packet.critic.model_dump(mode="json"),
                "citation_coverage": packet.citation_coverage,
                "claims": [{"claim_id": claim.claim_id, "statement": claim.statement,
                            "stance": claim.stance, "confidence": claim.confidence,
                            "supporting_evidence_ids": claim.supporting_evidence_ids,
                            "contradicting_evidence_ids": claim.contradicting_evidence_ids,
                            "unknowns": claim.unknowns} for claim in claims]}

    @staticmethod
    def _abstain_item(packet: ResearchPacket, reason: str) -> IntentItem:
        return IntentItem(symbol=packet.symbol, action="abstain", strength=1, priority=1,
                          rationale=reason, no_trade_reason=reason)
