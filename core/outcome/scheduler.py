"""Outcome 的日常调度边界：扫描账本，不参与研究决策或下单。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from core.collect.market import load_price_frame
from core.database.contracts import OutcomeCard, PositionEpisode, ReviewLabel
from core.database.raw import RawPayloadStore
from core.database.store import ResearchLedger

from .calculator import OutcomeCalculator

if TYPE_CHECKING:
    from agents.research.types import JsonModel


TerminalReason = Literal["horizon", "early_exit", "thesis_invalidated"]


@dataclass(frozen=True)
class OutcomeScheduleResult:
    """一次 scheduler 扫描实际写入账本的终结案例。"""

    outcomes: tuple[OutcomeCard, ...]
    review_labels: tuple[ReviewLabel, ...]
    episodes: tuple[PositionEpisode, ...]


class OutcomeScheduler:
    """每日收盘后由外部 scheduler 调用的 episode 结算器。"""

    def __init__(self, *, ledger: ResearchLedger, raw_store: RawPayloadStore, model: JsonModel) -> None:
        self._ledger = ledger
        self._store = raw_store
        self._model = model

    def scan(self, *, as_of: datetime, trace_prefix: str = "outcome-scheduler",
             episode_ids: tuple[str, ...] | None = None,
             terminal_reason: TerminalReason | None = None,
             market_regime: str | None = None,
             trigger_evidence_ids: tuple[str, ...] = ()) -> OutcomeScheduleResult:
        """扫描指定或全部 active episode，并在客观结算后立即触发 ReviewAgent。"""
        if as_of.tzinfo is None:
            raise ValueError("outcome scheduler as_of must be timezone-aware")
        requested = set(episode_ids or ())
        if terminal_reason is not None and not requested:
            raise ValueError("explicit terminal reason requires explicit episode_ids")
        if terminal_reason == "thesis_invalidated" and not trigger_evidence_ids:
            raise ValueError("thesis_invalidated requires frozen trigger evidence IDs")
        trace_id = f"{trace_prefix}-{as_of.strftime('%Y%m%d%H%M%S')}"
        self._ledger.start_trace(trace_id, as_of)
        outcomes: list[OutcomeCard] = []
        labels: list[ReviewLabel] = []
        settled: list[PositionEpisode] = []
        try:
            for episode in self._ledger.list_active_position_episodes():
                if requested and episode.episode_id not in requested:
                    continue
                fills = tuple(self._ledger.get_fill(fill_id) for fill_id in episode.fill_ids)
                reason = terminal_reason or self._automatic_reason(episode, fills, as_of)
                if reason is None:
                    continue
                if reason == "thesis_invalidated":
                    origin_plan = self._ledger.get_order_plan(episode.order_plan_ids[-1])
                    allowed = set(self._ledger.get_snapshot(origin_plan.snapshot_id).evidence_ids)
                    if not set(trigger_evidence_ids).issubset(allowed):
                        raise ValueError("thesis invalidation evidence must belong to the episode snapshot")
                built = OutcomeCalculator().build(
                    trace_id=trace_id, episode=episode, fills=fills, terminal_reason=reason,
                    as_of=as_of, prices=self._price_frame(episode, as_of), market_regime=market_regime,
                    trigger_evidence_ids=trigger_evidence_ids if reason == "thesis_invalidated" else (),
                )
                if built.outcome is None:
                    continue
                self._ledger.append_outcome_card(built.outcome)
                terminal = episode.model_copy(update={
                    "trace_id": trace_id, "created_at": as_of, "revision": episode.revision + 1,
                    "status": "terminal", "outcome_card_id": built.outcome.outcome_card_id,
                    "terminal_reason": reason,
                })
                self._ledger.append_position_episode(terminal)
                label = self._review(trace_id, terminal, built.outcome)
                self._ledger.append_review_label(label)
                completed = terminal.model_copy(update={
                    "trace_id": trace_id, "created_at": as_of, "revision": terminal.revision + 1,
                    "review_label_id": label.review_label_id,
                })
                self._ledger.append_position_episode(completed)
                outcomes.append(built.outcome)
                labels.append(label)
                settled.append(completed)
            self._ledger.complete_trace(trace_id, as_of)
        except Exception:
            self._ledger.fail_trace(trace_id, as_of)
            raise
        return OutcomeScheduleResult(tuple(outcomes), tuple(labels), tuple(settled))

    @staticmethod
    def _automatic_reason(episode: PositionEpisode, fills, as_of: datetime) -> TerminalReason | None:
        net_shares = sum(fill.filled_shares if fill.side == "buy" else -fill.filled_shares for fill in fills)
        if net_shares == 0 and any(fill.side == "sell" for fill in fills):
            return "early_exit"
        return "horizon" if as_of >= episode.current_due_at else None

    def _review(self, trace_id: str, episode: PositionEpisode, outcome: OutcomeCard) -> ReviewLabel:
        from agents.outcome import ReviewAgent

        # Review 只解释计划、执行与客观结果；研究证据仍由上游账本负责回放。
        context = self._ledger.materialize_review_context(episode)
        return ReviewAgent(model=self._model).review(
            trace_id=trace_id, episode=episode, outcome=outcome, context=context)

    def _price_frame(self, episode: PositionEpisode, as_of: datetime):
        frame = load_price_frame(episode.symbol, episode.anchor_at.strftime("%Y-%m-%d"),
                                 as_of.strftime("%Y-%m-%d"), store=self._store)
        frame = frame[frame["available_at"] <= as_of] if not frame.empty else frame
        if frame.empty:
            raise ValueError(f"missing PIT review prices for {episode.symbol}")
        return frame
