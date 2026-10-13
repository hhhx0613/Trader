"""阶段三 Committee 离线验收 / Offline Committee boundary tests."""

from datetime import datetime, timedelta, timezone

from agents.research import Committee
from core.database import ClaimCard, DataGateway, ResearchLedger, ResearchPacket, ResearchSnapshot
from core.database.contracts import (CriticVerdict, Fill, IntentItem, OrderPlan, OutcomeCard,
                                     PlannedOrder, PortfolioIntent, PositionEpisode, ReviewLabel)


AS_OF = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)


class CommitteeModel:
    def chat_json(self, _message, **_kwargs):
        return {"items": [
            {"symbol": "AAPL", "action": "long", "strength": 2, "priority": 1, "horizon_days": 20, "horizon_action": "start",
             "supporting_card_ids": ["cl_aapl"], "opposing_card_ids": [], "rationale": "cited support"},
            {"symbol": "MSFT", "action": "abstain", "strength": 1, "priority": 2,
             "supporting_card_ids": [], "opposing_card_ids": ["cl_msft"], "rationale": "critic abstained",
             "no_trade_reason": "critic abstained"},
        ]}


class BypassCriticModel:
    """只对 Critic 已弃权的 MSFT 越界；AAPL 意图仍合规，矫正不应株连它。"""

    def chat_json(self, _message, **_kwargs):
        return {"items": [
            {"symbol": "AAPL", "action": "long", "strength": 2, "priority": 1, "horizon_days": 20, "horizon_action": "start",
             "supporting_card_ids": ["cl_aapl"], "opposing_card_ids": [], "rationale": "cited support"},
            {"symbol": "MSFT", "action": "long", "strength": 3, "priority": 2,
             "supporting_card_ids": ["cl_msft"], "opposing_card_ids": [], "rationale": "disagree with critic"},
        ]}


class MissingSymbolModel:
    """响应集合不齐：生成基础失效，没有任何 item 值得单独采信。"""

    def chat_json(self, _message, **_kwargs):
        return {"items": [
            {"symbol": "AAPL", "action": "long", "strength": 2, "priority": 1, "horizon_days": 20, "horizon_action": "start",
             "supporting_card_ids": ["cl_aapl"], "opposing_card_ids": [], "rationale": "cited support"},
        ]}


class MemoryAwareModel(CommitteeModel):
    def __init__(self):
        self.message = ""

    def chat_json(self, message, **kwargs):
        self.message = message
        return super().chat_json(message, **kwargs)


def _build_fixture(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    ledger.start_trace("snapshot", AS_OF)
    snapshot = ResearchSnapshot(trace_id="snapshot", as_of=AS_OF, symbols=["AAPL", "MSFT"],
                                source_versions={}, evidence_ids=[], account_state={"cash": 1000})
    ledger.append_snapshot(snapshot)
    ledger.complete_trace("snapshot", AS_OF)

    ledger.start_trace("research", AS_OF)
    claim_aapl = ClaimCard(claim_id="cl_aapl", trace_id="research", agent="event", symbol="AAPL",
                           statement="demand improves", stance="bullish", confidence=0.7,
                           supporting_evidence_ids=["ev_aapl"])
    claim_msft = ClaimCard(claim_id="cl_msft", trace_id="research", agent="market", symbol="MSFT",
                           statement="volatility elevated", stance="bearish", confidence=0.6,
                           supporting_evidence_ids=["ev_msft"])
    ledger.append_claim(claim_aapl)
    ledger.append_claim(claim_msft)
    aapl = ResearchPacket(trace_id="research", symbol="AAPL", snapshot_id=snapshot.snapshot_id,
                          claim_card_ids=[claim_aapl.claim_id], citation_coverage=1.0,
                          critic=CriticVerdict(verdict="allow", reasons=["evidence consistent"]))
    msft = ResearchPacket(trace_id="research", symbol="MSFT", snapshot_id=snapshot.snapshot_id,
                          claim_card_ids=[claim_msft.claim_id], citation_coverage=1.0,
                          critic=CriticVerdict(verdict="abstain", reasons=["conflicting evidence"]))
    ledger.append_packet(aapl)
    ledger.append_packet(msft)
    ledger.complete_trace("research", AS_OF)
    return ledger, snapshot, aapl, msft


def test_committee_persists_thesis_and_cannot_bypass_abstaining_critic(tmp_path):
    ledger, snapshot, aapl, msft = _build_fixture(tmp_path)

    intent = Committee(ledger=ledger, gateway=DataGateway(ledger), model=CommitteeModel()).run(
        snapshot_id=snapshot.snapshot_id, packet_ids=(aapl.packet_id, msft.packet_id), as_of=AS_OF,
        trace_id="committee")
    items = {item.symbol: item for item in intent.items}
    assert items["AAPL"].action == "long"
    assert items["MSFT"].action == "abstain"
    assert ledger.get_intent(intent.intent_id).snapshot_id == snapshot.snapshot_id


def test_committee_injects_verified_episode_memory_by_target_symbol(tmp_path):
    ledger, snapshot, aapl, msft = _build_fixture(tmp_path)
    # 旧案例的时间锚必须严格早于 AS_OF：决策时点后才完成的 Outcome/Review 属于未来信息，会被 PIT 过滤挡掉。
    ledger.start_trace("memory", AS_OF)
    old_item = IntentItem(intent_item_id="ii_old", symbol="AAPL", action="long", strength=1, priority=1,
                          horizon_days=20, rationale="historical entry")
    ledger.append_intent(PortfolioIntent(trace_id="memory", snapshot_id=snapshot.snapshot_id, items=(old_item,)))
    ledger.append_order_plan(OrderPlan(trace_id="memory", order_plan_id="op_old",
                                       projected_portfolio_id="old_portfolio", snapshot_id=snapshot.snapshot_id,
                                       orders=(PlannedOrder(symbol="AAPL", side="buy", target_weight=0.1),)))
    fill = Fill(trace_id="memory", fill_id="fill_old", order_plan_id="op_old", symbol="AAPL", side="buy",
                decision_at=AS_OF - timedelta(days=30), executed_at=AS_OF - timedelta(days=29),
                requested_shares=1, filled_shares=1, executed_price=100, commission=0, status="filled")
    ledger.append_fill(fill)
    outcome = OutcomeCard(trace_id="memory", source_trace_id="old_portfolio", episode_id="ep_old",
                          symbol="AAPL", observation="horizon", observed_at=AS_OF - timedelta(days=2),
                          anchor_at=AS_OF - timedelta(days=30),
                          entry_price=100, mark_price=110, filled_shares=1, fill_ids=("fill_old",),
                          gross_return=.1, net_return=.1, max_drawdown=-.02, commission=0,
                          market_regime=None)
    ledger.append_outcome_card(outcome)
    label = ReviewLabel(trace_id="memory", outcome_card_id=outcome.outcome_card_id,
                        status="complete", rationale="historical reflection",
                        memory_eligibility="eligible",
                        created_at=AS_OF - timedelta(days=1))
    ledger.append_review_label(label)
    ledger.append_position_episode(PositionEpisode(trace_id="memory", episode_id="ep_old", revision=1,
                                                   symbol="AAPL", source_trace_id="old_portfolio", status="terminal",
                                                   anchor_at=AS_OF - timedelta(days=30),
                                                   current_due_at=AS_OF - timedelta(days=2), original_horizon_days=20,
                                                   intent_item_ids=("ii_old",), order_plan_ids=("op_old",),
                                                   fill_ids=("fill_old",), outcome_card_id=outcome.outcome_card_id,
                                                   review_label_id=label.review_label_id, terminal_reason="horizon"))
    ledger.complete_trace("memory", AS_OF)
    model = MemoryAwareModel()
    Committee(ledger=ledger, gateway=DataGateway(ledger), model=model).run(
        snapshot_id=snapshot.snapshot_id, packet_ids=(aapl.packet_id, msft.packet_id), as_of=AS_OF,
        trace_id="committee_memory")
    assert "ep_old" in model.message
    assert "historical entry" in model.message
    assert "historical reflection" in model.message


def test_local_violation_corrects_only_the_offending_symbol(tmp_path):
    ledger, snapshot, aapl, msft = _build_fixture(tmp_path)
    intent = Committee(ledger=ledger, gateway=DataGateway(ledger), model=BypassCriticModel()).run(
        snapshot_id=snapshot.snapshot_id, packet_ids=(aapl.packet_id, msft.packet_id), as_of=AS_OF,
        trace_id="committee_bypass")
    items = {item.symbol: item for item in intent.items}
    assert items["AAPL"].action == "long"
    assert items["MSFT"].action == "abstain"
    assert "bypass an abstaining critic" in items["MSFT"].no_trade_reason
    # 反静默篡改：被矫正的原始响应必须入账，重放能还原模型真正说过什么。
    records = [record for record in ledger.list_tool_calls("committee_bypass")
               if record["tool_name"] == "raw_response"]
    assert len(records) == 1
    assert records[0]["node"] == "committee" and records[0]["result_count"] == 1
    assert records[0]["evidence_ids"] == ["MSFT"]
    assert records[0]["arguments"]["violations"] == ["MSFT: cannot bypass an abstaining critic"]
    bypassed = {item["symbol"]: item["action"] for item in records[0]["arguments"]["response"]["items"]}
    assert bypassed["MSFT"] == "long"


def test_structural_violation_keeps_whole_batch_abstain(tmp_path):
    ledger, snapshot, aapl, msft = _build_fixture(tmp_path)
    intent = Committee(ledger=ledger, gateway=DataGateway(ledger), model=MissingSymbolModel()).run(
        snapshot_id=snapshot.snapshot_id, packet_ids=(aapl.packet_id, msft.packet_id), as_of=AS_OF,
        trace_id="committee_missing")
    items = {item.symbol: item for item in intent.items}
    assert {item.action for item in items.values()} == {"abstain"}
    assert "every packet symbol" in items["AAPL"].no_trade_reason
    records = [record for record in ledger.list_tool_calls("committee_missing")
               if record["tool_name"] == "raw_response"]
    assert records[0]["result_count"] == 1
