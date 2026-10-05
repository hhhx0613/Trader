"""行情 Agent / Interpretation of program-computed frozen market facts."""

from .claim_agent import ClaimAgent
from .types import ResearchState


class MarketAgent(ClaimAgent):
    """只解释 Gateway 行情切片；价格和技术指标必须由上游程序计算。"""

    name = "market"
    prompt_name = "market"

    def __call__(self, state: ResearchState) -> dict:
        # Market 仅解释本标的行情卡；跨维度比较留给 Critic/Committee，避免先验叙事锚定独立行情判断。
        common = {"snapshot_id": state["snapshot_id"], "symbol": state["symbol"], "as_of": state["as_of"]}
        evidence = self.gateway.get_market_slice(**common)
        claims, route = self._claim(state,
                               instruction="Assess trend, volatility, liquidity, drawdown, and priced-in risk from the present fields.",
                               evidence=evidence, allowed_kinds=("market",))
        return {"market_route": route, **({"market_claims": claims} if claims else {})}
