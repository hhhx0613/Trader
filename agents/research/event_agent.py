"""新闻事件 Agent / Cited event interpretation over frozen news."""

from core.research import ReasonCode
from core.research.contracts import RouteDecision

from .claim_agent import ClaimAgent
from .types import ResearchState


class EventAgent(ClaimAgent):
    """仅用冻结新闻形成事件论点，不生成交易建议。"""

    name = "event"
    prompt_name = "event"

    def __call__(self, state: ResearchState) -> dict:
        # 注入 hook：调用模型前无条件把本周冻结新闻拼进 prompt，不再让模型花一轮工具去要。
        evidence = self.gateway.get_news_batch(snapshot_id=state["snapshot_id"], symbol=state["symbol"],
                                                as_of=state["as_of"])
        if not evidence:
            return {"event_route": RouteDecision(node=self.name, route="abstain",
                    reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, detail="no frozen news evidence")}
        claims, route = self._claim(state, instruction="Classify relevance, novelty, catalyst and transmission path.",
                                   evidence=evidence, allowed_kinds=("news",))
        return {"event_route": route, **({"event_claims": claims} if claims else {})}
