"""财报基本面 Agent / Filing-only verification of an event thesis."""

from core.research import ReasonCode
from core.research.contracts import RouteDecision

from .claim_agent import ClaimAgent
from .types import ResearchState


class FundamentalAgent(ClaimAgent):
    """只读取 Gateway 披露/XBRL 摘录；新闻不能替代独立财报证据。"""

    name = "fundamental"
    prompt_name = "fundamental"

    def __call__(self, state: ResearchState) -> dict:
        event_claims = state.get("event_claims")
        if not event_claims:
            # 无事件论点则没有核验对象：Fundamental 只在 Event 立论后独立核验披露，不自行另起论点。
            return {}
        # Snapshot 无披露证据时直接弃权，一次模型都不调：避免工具空转与枚举式 section 重试。
        counts = self.gateway.kind_counts(snapshot_id=state["snapshot_id"], symbol=state["symbol"], as_of=state["as_of"])
        if counts.get("filing", 0) == 0:
            return {"fundamental_route": RouteDecision(node=self.name, route="abstain",
                    reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, detail="no filing evidence in frozen snapshot")}
        evidence = self.gateway.get_filing_section(snapshot_id=state["snapshot_id"], symbol=state["symbol"],
                                                    as_of=state["as_of"], section="")
        upstream = tuple(c.claim_id for c in event_claims)
        claims, route = self._claim(state, instruction="Verify the upstream event claims with independent filing evidence.",
                               evidence=evidence, upstream=upstream, allowed_kinds=("filing",))
        return {"fundamental_route": route, **({"fundamental_claims": claims} if claims else {})}
