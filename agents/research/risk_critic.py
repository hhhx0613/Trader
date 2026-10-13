"""研究风险批评 Agent / Soft research veto over upstream ClaimCards."""

from core.database import ReasonCode
from core.database.contracts import CriticVerdict

from .types import JsonModel, ResearchState
from .prompting import load_prompt


class RiskCritic:
    """只给出研究软否决，不执行组合硬约束、成本门控或订单操作。"""

    def __init__(self, *, gateway, model: JsonModel) -> None:
        self.gateway = gateway
        self.model = model
        self.prompt, self.prompt_version = load_prompt("risk_critic")

    def __call__(self, state: ResearchState) -> dict:
        claims: list = []
        for key in ("event_claims", "fundamental_claims", "market_claims"):
            claims.extend(state.get(key) or [])
        if not claims:
            return {"critic": CriticVerdict(verdict="abstain", reasons=["no valid upstream claims"])}
        try:
            prompt = {"prompt_version": self.prompt_version,
                      "claims": [item.model_dump(mode="json") for item in claims]}
            # 注入 hook：仓位/换手成本/regime/peer 本就是每轮必读的固定上下文，直接拼装后单轮定夺，
            # 不再走 function calling：彻底消除“工具跑完最后一轮吐空”的翻车回合。
            common = {"snapshot_id": state["snapshot_id"], "symbol": state["symbol"], "as_of": state["as_of"]}
            prompt["portfolio"] = self.gateway.get_current_portfolio(**common)
            prompt["turnover_cost"] = self.gateway.estimate_turnover_cost(**common)
            prompt["regime"] = self.gateway.get_regime(**common)
            prompt["peer_comparison"] = self.gateway.get_peer_comparison(**common)
            # deepseek-flash 为混合推理模型：max_tokens 过小会被内部思考 token 吃光、content 返回空。
            response = self.model.chat_json(str(prompt), system_prompt=self.prompt,
                                            temperature=0.0, max_tokens=8000, max_retries=1)
            verdict = CriticVerdict(verdict=response["verdict"], reasons=tuple(response["reasons"]),
                                    card_ids=tuple(response.get("card_ids", ())))
            if not set(verdict.card_ids) <= {claim.claim_id for claim in claims}:
                raise ValueError("critic cited a claim outside the current research state")
        except Exception:
            verdict = CriticVerdict(verdict="abstain", reasons=[str(ReasonCode.MODEL_FAILURE)])
        return {"critic": verdict}
