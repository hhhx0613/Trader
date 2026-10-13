"""OutcomeReviewFlow：一个 PositionEpisode 只在终局产生一张 OutcomeCard。"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agents.outcome import ReviewAgent
from core.outcome import OutcomeCalculator
from core.database.contracts import (
    ClaimCard,
    EvidenceCard,
    Fill,
    IntentItem,
    OrderPlan,
    PlannedOrder,
    PortfolioIntent,
    PositionEpisode,
    RawReference,
)
from core.database.reasons import ReasonCode
from core.database.store import ResearchLedger


UTC = timezone.utc
T0 = datetime(2026, 10, 1, 20, tzinfo=UTC)
T1 = T0 + timedelta(days=1)


def _prices(days: int = 21) -> pd.DataFrame:
    index = pd.date_range("2026-10-02", periods=days, freq="B")
    return pd.DataFrame({"open": [100.0 + i for i in range(days)],
                         "close": [101.0 + i for i in range(days)],
                         "available_at": [T1 + timedelta(days=i, hours=2) for i in range(days)]}, index=index)


def _fill(*, side: str = "buy", fill_id: str = "fill_1") -> Fill:
    return Fill(trace_id="execution", fill_id=fill_id, order_plan_id="op_outcome", symbol="AAPL",
                side=side, decision_at=T0, executed_at=T1, requested_shares=10, filled_shares=10,
                executed_price=100.0 if side == "buy" else 108.0, commission=1.0, status="filled")


def _episode(*, fill_ids=("fill_1",), due_at=datetime(2026, 10, 9, 20, tzinfo=UTC)) -> PositionEpisode:
    return PositionEpisode(trace_id="execution", episode_id="ep_1", symbol="AAPL", source_trace_id="portfolio",
                           status="active", anchor_at=T1, current_due_at=due_at, original_horizon_days=5,
                           intent_item_ids=("ii_1",), order_plan_ids=("op_outcome",), fill_ids=fill_ids)


def test_horizon_builds_one_actual_result_from_episode_fills():
    result = OutcomeCalculator().build(trace_id="review", episode=_episode(), fills=(_fill(),),
                                  terminal_reason="horizon", as_of=datetime(2026, 11, 1, tzinfo=UTC),
                                  prices=_prices())
    assert result.reason_code is None
    assert result.outcome is not None
    assert result.outcome.episode_id == "ep_1"
    assert result.outcome.observation == "horizon"
    assert result.outcome.fill_ids == ("fill_1",)
    assert result.outcome.net_return == pytest.approx(.059)


def test_not_due_and_no_fill_do_not_create_selection_results():
    calculator = OutcomeCalculator()
    not_due = calculator.build(trace_id="review", episode=_episode(), fills=(_fill(),), terminal_reason="horizon",
                          as_of=datetime(2026, 10, 6, 23, tzinfo=UTC), prices=_prices())
    assert not_due.outcome is None and not_due.reason_code == ReasonCode.OUTCOME_NOT_DUE
    no_fill = calculator.build(trace_id="review", episode=_episode(fill_ids=()), fills=(), terminal_reason="horizon",
                          as_of=datetime(2026, 11, 1, tzinfo=UTC), prices=_prices())
    assert no_fill.outcome is None and no_fill.reason_code == ReasonCode.OUTCOME_DATA_MISSING


def test_early_exit_uses_actual_exit_fill():
    exit_fill = _fill(side="sell", fill_id="fill_exit")
    result = OutcomeCalculator().build(trace_id="review", episode=_episode(fill_ids=("fill_1", "fill_exit")),
                                  fills=(_fill(), exit_fill), terminal_reason="early_exit",
                                  as_of=datetime(2026, 11, 1, tzinfo=UTC), prices=_prices())
    assert result.outcome is not None
    assert result.outcome.observation == "early_exit"
    assert result.outcome.mark_price == 108.0
    assert result.outcome.filled_shares == 0


class _ReviewModel:
    def __init__(self):
        self.messages: list[str] = []

    def chat_json(self, message, *args, **kwargs):
        self.messages.append(str(message))
        return {"reflection": "The completed episode is a bounded historical calibration case.",
                "memory_eligibility": "eligible"}


def _review_context() -> dict:
    """物化上下文的最小样例：形状与 ResearchLedger.materialize_review_context 一致。"""
    return {
        "original_decisions": [{"action": "long", "strength": 2, "horizon_days": 5,
                                "horizon_action": "start", "rationale": "filing-backed entry",
                                "supporting_card_ids": ("cl_1",), "opposing_card_ids": ()}],
        "execution": {"orders": [{"side": "buy", "target_weight": 0.1, "forced": False, "reason_codes": []}],
                      "fills": [{"fill_id": "fill_1", "side": "buy", "status": "filled",
                                 "filled_shares": 10, "executed_price": 100.0, "commission": 1.0,
                                 "reason_codes": [], "executed_at": T1.isoformat()}]},
    }


def test_ledger_revisions_preserve_episode_aggregate_and_retrievable_completion(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    episode = _episode()
    fill = _fill()
    ledger.start_trace("execution", T1)
    ledger.append_fill(fill)
    ledger.append_position_episode(episode)
    ledger.complete_trace("execution", T1)
    outcome = OutcomeCalculator().build(trace_id="review", episode=episode, fills=(fill,), terminal_reason="horizon",
                                   as_of=datetime(2026, 11, 1, tzinfo=UTC), prices=_prices()).outcome
    assert outcome is not None
    ledger.start_trace("review", datetime(2026, 11, 1, tzinfo=UTC))
    ledger.append_outcome_card(outcome)
    terminal = episode.model_copy(update={"trace_id": "review", "revision": 2, "status": "terminal",
                                           "outcome_card_id": outcome.outcome_card_id, "terminal_reason": "horizon"})
    ledger.append_position_episode(terminal)
    model = _ReviewModel()
    label = ReviewAgent(model=model).review(
        trace_id="review", episode=terminal, outcome=outcome, context=_review_context())
    assert "filing-backed entry" in model.messages[0]  # 输入补强：原始决策已随 context 物化
    assert "ev_ok" not in model.messages[0]  # Outcome Review 不接收研究证据或其引用白名单
    ledger.append_review_label(label)
    completed = terminal.model_copy(update={"revision": 3, "review_label_id": label.review_label_id})
    ledger.append_position_episode(completed)
    ledger.complete_trace("review", datetime(2026, 11, 1, tzinfo=UTC))
    assert ledger.get_position_episode("ep_1").review_label_id == label.review_label_id
    assert label.memory_eligibility == "eligible"
    assert ledger.list_verified_memory_episodes() == (completed,)


def test_complete_review_defaults_to_reject_until_memory_eligibility_is_assessed():
    from core.database.contracts import ReviewLabel

    label = ReviewLabel(trace_id="review", outcome_card_id="outcome_1", status="complete",
                        rationale="auditable reflection")

    assert label.memory_eligibility == "reject"


def test_review_context_materializes_decision_chain(tmp_path):
    ledger = ResearchLedger(tmp_path / "ledger.db")
    ledger.start_trace("execution", T1)
    ledger.start_trace("portfolio", T1)
    ledger.append_evidence(EvidenceCard(
        trace_id="portfolio", evidence_id="ev_1", kind="news", symbol="AAPL", available_at=T1,
        source="wire", raw=RawReference(sha256="ab" * 32, relative_path="news://" + "ab" * 32,
                                        source="news", received_at=T1), content_hash="cd" * 32))
    ledger.append_claim(ClaimCard(
        trace_id="portfolio", claim_id="cl_1", agent="event", symbol="AAPL",
        statement="upstream claim held", stance="bullish", confidence=0.5,
        supporting_evidence_ids=("ev_1",)))
    ledger.append_intent(PortfolioIntent(
        trace_id="portfolio", snapshot_id="snap_1", items=(IntentItem(
            intent_item_id="ii_1", symbol="AAPL", action="long", strength=2, priority=1,
            horizon_days=5, horizon_action="start", supporting_card_ids=("cl_1",),
            rationale="filing-backed entry"),)))
    ledger.append_order_plan(OrderPlan(
        trace_id="portfolio", order_plan_id="op_1", projected_portfolio_id="rpp_1",
        snapshot_id="snap_1", orders=(PlannedOrder(symbol="AAPL", side="buy", target_weight=0.1),)))
    ledger.append_fill(_fill())
    episode = _episode().model_copy(update={"order_plan_ids": ("op_1",)})
    ledger.append_position_episode(episode)

    context = ledger.materialize_review_context(episode)

    assert context["original_decisions"][0]["action"] == "long"
    assert context["execution"]["orders"][0]["side"] == "buy"
    assert context["execution"]["fills"][0]["fill_id"] == "fill_1"
    assert "claims" not in context and "evidence_catalog" not in context
