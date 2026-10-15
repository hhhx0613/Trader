"""Review Agent 的窄输入边界：没有交易和数据访问能力。"""

from __future__ import annotations

import json
from typing import Any

from agents.research.types import JsonModel
from agents.research.prompting import load_prompt
from core.database.contracts import OutcomeCard, PositionEpisode, ReviewLabel
from core.database.reasons import ReasonCode


class ReviewAgent:
    def __init__(self, *, model: JsonModel) -> None:
        self._model = model
        self.prompt, self.prompt_version = load_prompt("review")

    def review(self, *, trace_id: str, episode: PositionEpisode, outcome: OutcomeCard,
               context: dict[str, Any]) -> ReviewLabel:
        """context 由调用方沿账本 ID 预物化（ResearchLedger.materialize_review_context）。"""
        message = json.dumps({
            "episode": {
                "episode_id": episode.episode_id,
                "symbol": episode.symbol,
                "anchor_at": episode.anchor_at.isoformat(),
                "current_due_at": episode.current_due_at.isoformat(),
                "original_horizon_days": episode.original_horizon_days,
                "terminal_reason": episode.terminal_reason,
            },
            **context,
            "outcome": outcome.model_dump(mode="json"),
        }, ensure_ascii=False, sort_keys=True)
        try:
            response = self._model.chat_json(message, system_prompt=self.prompt, temperature=0.0,
                                             max_tokens=800, max_retries=3)
            reflection = str(response.get("reflection", "")).strip()
            if not reflection:
                return self._abstain(trace_id, outcome, ReasonCode.SCHEMA_VIOLATION)
            memory_eligibility = str(response.get("memory_eligibility", "reject"))
            return ReviewLabel(trace_id=trace_id, outcome_card_id=outcome.outcome_card_id,
                               status="complete", prompt_version=self.prompt_version, rationale=reflection,
                               memory_eligibility=memory_eligibility,
                               created_at=outcome.observed_at)
        except Exception:
            return self._abstain(trace_id, outcome, ReasonCode.MODEL_FAILURE)

    @staticmethod
    def _abstain(trace_id: str, outcome: OutcomeCard, reason: ReasonCode) -> ReviewLabel:
        return ReviewLabel(trace_id=trace_id, outcome_card_id=outcome.outcome_card_id,
                           status="abstain", rationale="review unavailable or output schema rejected",
                           prompt_version=None,
                           reason_codes=(reason,), memory_eligibility="reject", created_at=outcome.observed_at)
