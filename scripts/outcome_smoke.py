#!/usr/bin/env python3
"""最小离线闭环：Fill -> Episode -> Scheduler/Review -> Committee 校准引用。

不联网、不读真实账户、不调用真实 LLM；价格读取在本脚本中替换为 PIT fixture，
因此只验证对象边界和时序，不替代滚动样本外实验。
"""
from __future__ import annotations

import sys
import shutil
import gc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.research.committee import Committee
from core.outcome import OutcomeScheduler
from core.database import DataGateway, ResearchLedger
from core.database.contracts import (ClaimCard, CriticVerdict, EvidenceCard, Fill, IntentItem, OrderPlan,
                                     PlannedOrder, PortfolioIntent, PositionEpisode, RawReference, ResearchPacket,
                                     ResearchSnapshot)
from core.database.raw import RawPayloadStore

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 20, tzinfo=UTC)
T1, T_REVIEW, T_NEXT = T0 + timedelta(days=1), datetime(2026, 10, 10, 20, tzinfo=UTC), datetime(2026, 10, 13, 20, tzinfo=UTC)


class ReviewModel:
    def chat_json(self, *_args, **_kwargs):
        return {"reflection": "Completed historical calibration case.", "evidence_ids": ["ev_origin"]}


class CommitteeModel:
    def __init__(self): self.message = ""
    def chat_json(self, message, **_kwargs):
        self.message = str(message)
        return {"items": [{"symbol": "AAPL", "action": "long", "strength": 2, "priority": 1,
            "horizon_action": "start", "horizon_days": 5, "supporting_card_ids": ["cl_next"],
            "opposing_card_ids": [], "calibration_episode_ids": ["ep_smoke"], "rationale": "fixture"}]}


def evidence(trace_id, evidence_id, at):
    return EvidenceCard(trace_id=trace_id, evidence_id=evidence_id, kind="news", symbol="AAPL", available_at=at,
        source="fixture", raw=RawReference(sha256="ab" * 32, relative_path="fixture://origin", source="fixture", received_at=at), content_hash="cd" * 32)


def prices():
    index = pd.date_range("2026-10-02", periods=7, freq="B")
    return pd.DataFrame({"open": [100 + i for i in range(7)], "close": [101 + i for i in range(7)],
                         "available_at": [T1 + timedelta(days=i, hours=1) for i in range(7)]}, index=index)


def main() -> int:
    # Windows 用户 Temp 在该环境可能无 SQLite ACL；使用 data/ 下受控且运行后清理的目录。
    temp = ROOT / "data" / "_outcome_smoke"
    if temp.exists():
        raise RuntimeError(f"temporary smoke directory already exists: {temp}")
    temp.mkdir()
    if True:
        ledger, raw = ResearchLedger(temp / "ledger.db"), RawPayloadStore(temp / "raw")
        try:
            ledger.start_trace("origin", T0)
            origin = ResearchSnapshot(trace_id="origin", snapshot_id="snap_origin", as_of=T0, symbols=("AAPL",), source_versions={}, evidence_ids=("ev_origin",), account_state={"cash": 1000.0, "positions": [], "regime": "risk-on"})
            ledger.append_snapshot(origin); ledger.append_evidence(evidence("origin", "ev_origin", T0))
            ledger.append_claim(ClaimCard(trace_id="origin", claim_id="cl_origin", agent="event", symbol="AAPL", statement="fixture", stance="bullish", confidence=.7, supporting_evidence_ids=("ev_origin",)))
            ledger.append_intent(PortfolioIntent(trace_id="origin", snapshot_id="snap_origin", items=(IntentItem(intent_item_id="ii_origin", symbol="AAPL", action="long", strength=2, priority=1, horizon_action="start", horizon_days=5, supporting_card_ids=("cl_origin",), rationale="fixture"),)))
            ledger.append_order_plan(OrderPlan(trace_id="origin", order_plan_id="op_origin", projected_portfolio_id="rpp", snapshot_id="snap_origin", orders=(PlannedOrder(symbol="AAPL", side="buy", target_weight=.1),)))
            ledger.complete_trace("origin", T0)
            ledger.start_trace("execution", T1)
            fill = Fill(trace_id="execution", fill_id="fill_origin", order_plan_id="op_origin", symbol="AAPL", side="buy", decision_at=T0, executed_at=T1, requested_shares=10, filled_shares=10, executed_price=100., commission=1., status="filled")
            ledger.append_fill(fill); ledger.append_position_episode(PositionEpisode(trace_id="execution", episode_id="ep_smoke", symbol="AAPL", source_trace_id="origin", status="active", anchor_at=T1, current_due_at=datetime(2026, 10, 9, 20, tzinfo=UTC), original_horizon_days=5, intent_item_ids=("ii_origin",), order_plan_ids=("op_origin",), fill_ids=("fill_origin",)))
            ledger.complete_trace("execution", T1)
            with patch("core.outcome.scheduler.load_price_frame", return_value=prices()):
                settled = OutcomeScheduler(ledger=ledger, raw_store=raw, model=ReviewModel()).scan(as_of=T_REVIEW)
            assert len(settled.outcomes) == len(settled.review_labels) == 1
            ledger.start_trace("next", T_NEXT)
            next_snapshot = ResearchSnapshot(trace_id="next", snapshot_id="snap_next", as_of=T_NEXT, symbols=("AAPL",), source_versions={}, evidence_ids=("ev_next",), account_state={"cash": 0., "positions": [{"symbol": "AAPL", "quantity": 10}], "regime": "risk-on"})
            ledger.append_snapshot(next_snapshot); ledger.append_evidence(evidence("next", "ev_next", T_NEXT))
            ledger.append_claim(ClaimCard(trace_id="next", claim_id="cl_next", agent="event", symbol="AAPL", statement="next", stance="bullish", confidence=.6, supporting_evidence_ids=("ev_next",)))
            packet = ResearchPacket(trace_id="next", symbol="AAPL", snapshot_id="snap_next", claim_card_ids=("cl_next",), citation_coverage=1., critic=CriticVerdict(verdict="allow", reasons=("fixture",)))
            ledger.append_packet(packet); ledger.complete_trace("next", T_NEXT)
            model = CommitteeModel(); intent = Committee(ledger=ledger, gateway=DataGateway(ledger), model=model).run(snapshot_id="snap_next", packet_ids=(packet.packet_id,), as_of=T_NEXT, trace_id="committee_next")
            assert "ep_smoke" in model.message and intent.items[0].calibration_episode_ids == ("ep_smoke",)
            print("PASS: Fill -> Episode -> Scheduler -> Outcome -> Review -> Committee calibration")
            return 0
        finally:
            raw.close()
            del ledger
            gc.collect()
            shutil.rmtree(temp)


if __name__ == "__main__":
    raise SystemExit(main())
