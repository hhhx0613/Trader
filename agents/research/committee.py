"""跨标的 Committee / Frozen ThesisBook to cited PortfolioIntent only."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from core.database import DataGateway, ResearchLedger
from core.database.contracts import IntentItem, PortfolioIntent, ResearchPacket, ThesisBook

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
        regime_label = (regime or {}).get("label")
        query_profiles = {
            packet.symbol: {
                "held": any(
                    str(row.get("symbol", "")).upper() == packet.symbol and int(row.get("quantity", 0)) > 0
                    for row in portfolio.get("positions", ())
                ),
                "agents": tuple(self.ledger.get_claim(claim_id).agent for claim_id in packet.claim_card_ids),
                "stances": tuple(self.ledger.get_claim(claim_id).stance for claim_id in packet.claim_card_ids),
            } for packet in packets
        }
        episodes_by_symbol, retrieval_audit = self.ledger.select_verified_memory_episodes(
            symbols=tuple(packet.symbol for packet in packets), regime=regime_label, as_of=as_of,
            query_profiles=query_profiles, limit_per_symbol=4)
        all_candidate_ids = tuple(episode.episode_id for episode in self.ledger.list_verified_memory_episodes())
        for symbol, episodes in episodes_by_symbol.items():
            self.ledger.record_tool_call(
                trace_id=trace_id, snapshot_id=snapshot_id, symbol=symbol, node="committee",
                tool_name="historical_episode_retrieval",
                arguments={"profile_version": "structured_nearest_v1", "regime": regime_label,
                           "candidate_episode_ids": all_candidate_ids,
                           "query_profile": query_profiles[symbol], "ranking": retrieval_audit[symbol],
                           "selected_episode_ids": tuple(episode.episode_id for episode in episodes)},
                result_count=len(episodes), evidence_ids=tuple(episode.episode_id for episode in episodes),
                requested_at=as_of,
            )
        active_by_symbol = {
            episode.symbol: {
                "episode_id": episode.episode_id,
                "anchor_at": episode.anchor_at,
                "current_due_at": episode.current_due_at,
                "original_horizon_days": episode.original_horizon_days,
            }
            for episode in self.ledger.list_active_position_episodes()
        }
        portfolio = {**portfolio, "active_episodes_by_symbol": active_by_symbol}
        return ThesisBook(trace_id=trace_id, snapshot_id=snapshot_id,
                          packet_ids=tuple(packet.packet_id for packet in packets),
                          portfolio_state=portfolio, regime=regime_label,
                          historical_episode_ids_by_symbol={
                              symbol: tuple(episode.episode_id for episode in episodes)
                              for symbol, episodes in episodes_by_symbol.items()
                          })

    def _decide(self, trace_id: str, book: ThesisBook, packets: tuple[ResearchPacket, ...],
                as_of: datetime) -> PortfolioIntent:
        packet_by_symbol = {packet.symbol: packet for packet in packets}
        historical_episodes = {
            symbol: [self._episode_digest(self.ledger.get_position_episode(episode_id)) for episode_id in episode_ids]
            for symbol, episode_ids in book.historical_episode_ids_by_symbol.items()
        }
        # 入 prompt 的只有决策摘要：book 的 ID 锚点留在账本，模型输出从不引用它们；
        # as_of 随 prompt 冻结，模型才能自检「新到期日严格早于 current_due_at」。
        prompt = {"prompt_version": self.prompt_version, "as_of": as_of.isoformat(),
                  "portfolio_state": self._portfolio_digest(book),
                  "regime": book.regime,
                  "packets": [self._packet_digest(packet) for packet in packets],
                  "historical_episodes_by_symbol": historical_episodes}
        try:
            # 输出 schema 已内联在 committee.txt：prompt 文件即完整 system prompt，代码不再拼接。
            response = self.model.chat_json(str(prompt), system_prompt=self.prompt, temperature=0.0,
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
            reason = self._item_violation(
                item, packet_by_symbol[item.symbol],
                set(book.historical_episode_ids_by_symbol.get(item.symbol, ())),
                active=self.ledger.find_active_position_episode(item.symbol), as_of=as_of,
            )
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
    def _item_violation(item: IntentItem, packet: ResearchPacket,
                        allowed_episode_ids: set[str], *, active, as_of: datetime) -> str | None:
        """单标的边界校验：违规理由即矫正记录，不回抛以免株连全池。

        两组规则：先查引用是否 grounded（只能引用本轮供给过的 claim/episode），
        再查动作与持有窗口是否和「该标的是否已有活动 episode」这一事实自洽。
        """
        # —— 引用合法性：模型不得幻觉出范围外的锚点，这是执法地基 ——
        cited = set(item.supporting_card_ids) | set(item.opposing_card_ids)
        if not cited <= set(packet.claim_card_ids):
            # 事实引用只能落在该标的自己的 Packet 内，禁止跨标的或编造 claim_id
            return "cited a claim outside its packet"
        if packet.critic.verdict in {"abstain", "human_review"} and item.action != "abstain":
            # Risk Critic 是软否决闸：弃权/转人工时委员会不得绕过它强行下动作
            return "cannot bypass an abstaining critic"
        if item.action != "abstain" and not item.supporting_card_ids:
            # 任何要动手的意图都必须有至少一条支持卡，杜绝无依据交易
            return "non-abstain intent requires a supporting ClaimCard"
        if not set(item.calibration_episode_ids).issubset(allowed_episode_ids):
            # 历史案例是校准依据而非事实依据：只能引用本轮为该标的供给过的 episode_id
            return "cited an episode outside this symbol's supplied historical episodes"
        if len(item.calibration_episode_ids) > 4:
            return "cited too many calibration episodes"
        # —— 生命周期一致性：按 action 与活动 episode 的有无分流 ——
        if item.action in {"exit", "abstain"}:
            # 清仓/不参与不再声明持有窗口，否则后续到期计算会引用一个不存在的新 horizon
            if item.horizon_action != "none" or item.horizon_days is not None:
                return "exit and abstain require horizon_action=none and horizon_days=null"
            return None
        if active is None:
            # 无活动 episode：只有 long 才是合法起点；hold/reduce 的前提是已持有，不能凭空减/持
            if item.action != "long":
                return "hold or reduce requires an existing active episode"
            # 新建论文必须显式选定 5/20/60 交易日桶之一，供 OutcomeCalculator 按同一口径结算
            if item.horizon_action != "start" or item.horizon_days not in {5, 20, 60}:
                return "a new thesis requires start with a 5, 20, or 60 trading-day horizon"
            return None
        if item.horizon_action == "retain" and item.horizon_days is None:
            # 已有活动仓位：保留原到期日即无需新的 horizon_days
            return None
        if item.horizon_action == "shorten" and item.horizon_days in {5, 20, 60}:
            # 系统对已有论文只允许提前、不允许延后到期，用业务日重算到期日做严格比较
            requested_due = pd.Timestamp(as_of) + pd.offsets.BDay(item.horizon_days)
            if requested_due.to_pydatetime() < active.current_due_at:
                return None
            return "shorten must set a due date strictly earlier than the active episode"
        return "an active thesis requires retain or a strictly earlier shorten horizon"

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

    def _portfolio_digest(self, book: ThesisBook) -> dict[str, Any]:
        """账户摘要只带决策数值与时间锚：活动 episode 的 ID 留在账本，模型输出从不引用。"""
        state = book.model_dump(mode="json")["portfolio_state"]
        state["active_episodes_by_symbol"] = {
            symbol: {"anchor_at": episode["anchor_at"], "current_due_at": episode["current_due_at"],
                     "original_horizon_days": episode["original_horizon_days"]}
            for symbol, episode in (state.get("active_episodes_by_symbol") or {}).items()
        }
        return state

    def _packet_digest(self, packet: ResearchPacket) -> dict[str, Any]:
        """claim 只给可引用字段与判断材料：证据 ID 是账本锚点，输出 schema 无从引用。"""
        claims = [self.ledger.get_claim(claim_id) for claim_id in packet.claim_card_ids]
        return {"symbol": packet.symbol, "critic": packet.critic.model_dump(mode="json"),
                "citation_coverage": packet.citation_coverage,
                "claims": [{"claim_id": claim.claim_id, "statement": claim.statement,
                            "stance": claim.stance, "confidence": claim.confidence,
                            "unknowns": claim.unknowns} for claim in claims]}

    def _episode_digest(self, episode) -> dict[str, Any]:
        """仅在 prompt 内物化 Episode；底层事实继续保留在账本对象中。"""
        outcome = self.ledger.get_outcome_card(episode.outcome_card_id)
        label = self.ledger.get_review_label(episode.review_label_id)
        intents = [self.ledger.get_intent_item(item_id) for item_id in episode.intent_item_ids]
        fills = [self.ledger.get_fill(fill_id) for fill_id in episode.fill_ids]
        return {
            "episode_id": episode.episode_id,
            "symbol": episode.symbol,
            "original_decisions": [{"action": item.action, "horizon_days": item.horizon_days,
                                    "rationale": item.rationale} for item in intents],
            "execution": {"fill_count": len(fills), "commission": sum(fill.commission for fill in fills)},
            "outcome": {"net_return": outcome.net_return, "max_drawdown": outcome.max_drawdown,
                        "terminal_reason": episode.terminal_reason, "market_regime": outcome.market_regime},
            "review_reflection": label.rationale,
        }

    @staticmethod
    def _abstain_item(packet: ResearchPacket, reason: str) -> IntentItem:
        return IntentItem(symbol=packet.symbol, action="abstain", strength=1, priority=1,
                          rationale=reason, no_trade_reason=reason)
