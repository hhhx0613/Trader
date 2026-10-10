"""行情 Agent / Interpretation of program-computed frozen market facts."""

import re

from .claim_agent import ClaimAgent
from .types import ResearchState


class MarketAgent(ClaimAgent):
    """只解释 Gateway 行情切片；价格和技术指标必须由上游程序计算。"""

    name = "market"
    prompt_name = "market"

    # Prompt 是第一道边界；这里拒绝典型的「引用教科书阈值」措辞，防止模型在没有
    # 程序给出阈值字段时把外部技术分析规则伪装成冻结市场事实。
    _UNINJECTED_THRESHOLD = re.compile(
        r"\b(?:common|conventional|typical|textbook|standard|meaningful)\s+"
        r"(?:technical(?:[- ]analysis)?\s+|trend(?:[- ]strength)?\s+)?threshold\b", re.IGNORECASE)

    def _accept_claim_item(self, item: dict) -> bool:
        text = " ".join(str(item.get(key, "")) for key in ("statement", "unknowns"))
        return not bool(self._UNINJECTED_THRESHOLD.search(text))

    def __call__(self, state: ResearchState) -> dict:
        # Market 仅解释本标的行情卡；跨维度比较留给 Critic/Committee，避免先验叙事锚定独立行情判断。
        common = {"snapshot_id": state["snapshot_id"], "symbol": state["symbol"], "as_of": state["as_of"]}
        evidence = self.gateway.get_market_slice(**common)
        claims, route = self._claim(state,
                               instruction="Assess trend, volatility, liquidity, drawdown, and priced-in risk from the present fields.",
                               evidence=evidence, allowed_kinds=("market",))
        return {"market_route": route, **({"market_claims": claims} if claims else {})}
