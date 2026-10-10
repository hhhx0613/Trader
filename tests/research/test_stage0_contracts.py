"""阶段 0 契约、账本与路由回归测试 / Stage-0 contract, ledger and routing regression tests.

全部离线运行，只覆盖「数据 -> 研究 -> Committee」链路的冻结契约：每个持久化
对象有对应账本表与 typed append/get 且可账本往返；缺关键字段、缺引用、越界
路由得到确定拒绝；原因码全局唯一且与 providers 共享；Committee 边界对象在
构造层面不存在权重或订单字段。组合/订单/复盘契约推迟到阶段 4-6 定义时再测。

All offline, covering only the data -> research -> Committee contracts. Each
persisted object round-trips through its ledger table via typed append/get.
Missing fields, missing citations, and unknown routes are rejected
deterministically. Reason codes are unique and shared with providers. Committee
boundary objects cannot carry weight or order fields at construction time.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import core.research.contracts as contracts_module
from core.research import (
    ClaimCard,
    EvidenceCard,
    PortfolioIntent,
    ReasonCode,
    ResearchLedger,
    ResearchPacket,
    ResearchSnapshot,
    RouteDecision,
    ThesisBook,
)
from core.research.contracts import (
    Contract, CriticVerdict, Fill, IntentConstraints, IntentItem, OrderPlan,
    RiskProjectedPortfolio, TargetPortfolio,
)
from core.research.providers import PITValidationError, validate_available_at
from core.research.store import OBJECT_TABLES

AS_OF = datetime(2026, 10, 3, 20, tzinfo=timezone.utc)
TRACE = "trace-0"


def critic():
    return CriticVerdict(verdict="allow", reasons=["no open conflict"], card_ids=["cl_ref"])


def intent_item(**updates):
    base = dict(symbol="AAPL", action="long", strength=2, priority=1, horizon_days=20, supporting_card_ids=["cl_ref"], rationale="supply deal confirmed")
    base.update(updates)
    return IntentItem(**base)


def samples():
    """每个持久化契约的 (表名, append/get 后缀, 主键字段, 实例) / one row per persisted contract."""
    claim = ClaimCard(trace_id=TRACE, agent="event", symbol="AAPL", statement="Deal lifts margin", stance="bullish", confidence=0.6, supporting_evidence_ids=["ev_1"])
    packet = ResearchPacket(trace_id=TRACE, symbol="AAPL", snapshot_id="snap_1", claim_card_ids=[claim.claim_id], citation_coverage=1.0, critic=critic())
    book = ThesisBook(trace_id=TRACE, snapshot_id="snap_1", packet_ids=[packet.packet_id], portfolio_state={"cash": 1000})
    intent = PortfolioIntent(trace_id=TRACE, snapshot_id="snap_1", items=[intent_item()])
    return [
        ("claim_cards", "claim", "claim_id", claim),
        ("research_packets", "packet", "packet_id", packet),
        ("thesis_books", "thesis_book", "thesis_book_id", book),
        ("portfolio_intents", "intent", "intent_id", intent),
    ]


@pytest.mark.parametrize("table, suffix, id_field, contract", samples())
def test_persisted_objects_roundtrip_via_ledger(tmp_path, table, suffix, id_field, contract):
    assert table in OBJECT_TABLES
    ledger = ResearchLedger(tmp_path / "ledger.db")
    ledger.start_trace(TRACE, AS_OF)
    getattr(ledger, f"append_{suffix}")(contract)
    restored = getattr(ledger, f"get_{suffix}")(getattr(contract, id_field))
    assert restored.model_dump(mode="json") == contract.model_dump(mode="json")


def test_only_documented_contracts_are_frozen():
    """禁止未在 Plan 中声明的并行契约 / no orphan contract subclasses."""
    found = {value for value in vars(contracts_module).values() if isinstance(value, type) and issubclass(value, Contract) and value is not Contract}
    assert found == {
        EvidenceCard, ResearchSnapshot, ClaimCard, ResearchPacket, ThesisBook, PortfolioIntent,
        IntentConstraints, TargetPortfolio, RiskProjectedPortfolio, OrderPlan, Fill,
    }


def test_claim_requires_supporting_citation():
    with pytest.raises(ValidationError, match="supporting"):
        ClaimCard(trace_id=TRACE, agent="event", symbol="AAPL", statement="rumor", stance="bullish", confidence=0.5)
    with pytest.raises(ValidationError, match="agent"):
        ClaimCard(trace_id=TRACE, symbol="AAPL", statement="deal", stance="bullish", confidence=0.5, supporting_evidence_ids=["ev_1"])


def test_critic_verdict_requires_reasons():
    with pytest.raises(ValidationError, match="reasons"):
        CriticVerdict(verdict="allow")


def test_committee_boundary_objects_cannot_carry_weights():
    with pytest.raises(ValidationError, match="Extra inputs"):
        PortfolioIntent(trace_id=TRACE, snapshot_id="snap_1", items=[intent_item()], weights={"AAPL": 0.5})
    with pytest.raises(ValidationError, match="Extra inputs"):
        intent_item(weight=0.5)
    for name in ("weights", "weight", "order", "price"):
        assert name not in PortfolioIntent.model_fields
        assert name not in IntentItem.model_fields


def test_intent_rejects_duplicate_symbols():
    with pytest.raises(ValidationError, match="unique"):
        PortfolioIntent(trace_id=TRACE, snapshot_id="snap_1", items=[intent_item(), intent_item()])


def test_route_contract_forces_explicit_branches():
    RouteDecision(node="committee", route="proceed")
    for route in ("abstain", "data_request", "human_review", "no_trade", "failed"):
        RouteDecision(node="event", route=route, reason_code=ReasonCode.INSUFFICIENT_EVIDENCE)
    with pytest.raises(ValidationError, match="reason_code"):
        RouteDecision(node="event", route="abstain")
    with pytest.raises(ValidationError):
        RouteDecision(node="event", route="buy", reason_code=ReasonCode.NO_TRADE_BAND)


def test_reason_codes_unique_and_shared_with_pit():
    values = [code.value for code in ReasonCode]
    assert len(values) == len(set(values))
    assert {"missing_available_at", "invalid_available_at", "timezone_required", "future_data", "missing_identity", "invalid_kind"} <= set(values)
    assert str(ReasonCode.FUTURE_DATA) == "future_data"


def test_provider_pit_rejections_emit_registered_codes():
    with pytest.raises(PITValidationError) as excinfo:
        validate_available_at({"available_at": AS_OF + timedelta(seconds=1)}, AS_OF)
    assert excinfo.value.code in set(ReasonCode)
    assert "future_data" in str(excinfo.value)
