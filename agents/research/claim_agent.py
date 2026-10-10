"""有引用 ClaimCard 的公共构造边界 / Shared cited-claim construction boundary."""

from __future__ import annotations

from typing import Any

from core.research import ClaimCard, DataGateway, ReasonCode, ResearchLedger
from core.research.contracts import RouteDecision

from .types import JsonModel, ResearchState
from .prompting import load_prompt
from .tools import ResearchTools

_MAX_CLAIMS_PER_AGENT = 5
# 类别取数已由各 Agent 的注入 hook 直接完成，模型侧只保留同维度定向补查这一个工具。
_RECHECK_TOOLS = ("search_evidence",)
_RECHECK_BUDGET = 2


class ClaimAgent:
    """三个事实解释 Agent 的共用安全壳：只接受 Gateway 摘录并校验模型引用。"""

    name: str
    prompt_name: str

    def __init__(self, *, ledger: ResearchLedger, gateway: DataGateway, model: JsonModel) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.model = model
        self.prompt, self.prompt_version = load_prompt(self.prompt_name)

    def _accept_claim_item(self, item: dict[str, Any]) -> bool:
        """子类可拒绝违反其证据解释边界的单条模型输出。"""
        return True

    def _claim(self, state: ResearchState, *, instruction: str, evidence: list[dict[str, Any]],
               upstream: tuple[str, ...] = (),
               context: dict[str, Any] | None = None,
               allowed_kinds: tuple[str, ...] = ("news", "filing", "market")) -> tuple[list[ClaimCard], RouteDecision]:
        """默认把注入 hook 取回的冻结批次拼进 prompt，模型单轮出 1-5 张 ClaimCard；仅保留 `search_evidence` 只读补查。"""
        prompt = {"agent": self.name, "prompt_version": self.prompt_version, "instruction": instruction,
                  "symbol": state["symbol"], "as_of": state["as_of"].isoformat(),
                  "upstream_claim_ids": upstream}
        # 证据在第一次模型调用前就已就位：不再要求模型花一轮工具去「要」它本就该看到的批次。
        prompt["evidence"] = [{key: item[key] for key in ("evidence_id", "source", "title", "summary", "text")}
                              for item in evidence]
        if context:
            prompt["context"] = context
        # 可引用集从注入批次起步；补查命中的卡片随后并入。
        ids = {item["evidence_id"] for item in evidence}
        try:
            schema = (self.prompt + '\n\n' + f'Return a JSON object with key "claims": an array of 1 to {_MAX_CLAIMS_PER_AGENT} claim objects. '
                      'Each claim object has keys statement (string), stance '
                      '(bullish, bearish, or neutral), confidence (number 0..1), '
                      'supporting_evidence_ids (non-empty array of only supplied evidence_id values), '
                      'contradicting_evidence_ids (array of only supplied evidence_id values, may be empty), '
                      'and unknowns (array of strings). Do not add keys to claim objects. '
                      'Group related evidence into one claim; separate independent catalysts into separate claims. '
                      'Judge primarily from the evidence already injected in this prompt; you may call the '
                      'read-only search_evidence tool once or twice to re-check other cards of the same evidence '
                      'kind frozen in this same snapshot. If even so no directional thesis is supported, '
                      'return exactly {"route":"data_request","reason_code":"insufficient_evidence",'
                      '"detail":"what frozen evidence is missing"} instead.')
            if hasattr(self.model, "chat_with_tools"):
                tools = ResearchTools(gateway=self.gateway, state=state, node=self.name,
                                      allowed_tools=_RECHECK_TOOLS, max_calls=_RECHECK_BUDGET,
                                      allowed_kinds=allowed_kinds)
                response = self.model.chat_with_tools(str(prompt), system_prompt=schema, tools=tools.tools,
                                                      tool_executor=tools.execute, max_tool_calls=_RECHECK_BUDGET,
                                                      temperature=0.0, max_tokens=6000)
                ids |= tools.evidence_ids
            else:
                response = self.model.chat_json(str(prompt), system_prompt=schema, temperature=0.0,
                                                max_tokens=6000, max_retries=0)
            if not isinstance(response, dict):
                return [], RouteDecision(node=self.name, route="abstain", reason_code=ReasonCode.SCHEMA_VIOLATION,
                                          detail="model output is not a JSON object")
            if response.get("route") == "data_request":
                if response.get("reason_code") != str(ReasonCode.INSUFFICIENT_EVIDENCE):
                    return [], RouteDecision(node=self.name, route="abstain", reason_code=ReasonCode.SCHEMA_VIOLATION,
                                              detail="data_request has an unsupported reason code")
                return [], RouteDecision(node=self.name, route="data_request",
                                          reason_code=ReasonCode.INSUFFICIENT_EVIDENCE,
                                          detail=str(response.get("detail", "model requested more frozen evidence")))
            raw_claims = response.get("claims")
            if not isinstance(raw_claims, list) or not raw_claims:
                return [], RouteDecision(node=self.name, route="abstain", reason_code=ReasonCode.SCHEMA_VIOLATION,
                                          detail="model returned no claims array")
            cards: list[ClaimCard] = []
            for item in raw_claims[:_MAX_CLAIMS_PER_AGENT]:
                if not isinstance(item, dict) or not self._accept_claim_item(item):
                    continue
                cited = tuple(item.get("supporting_evidence_ids", ()))
                opposing = tuple(item.get("contradicting_evidence_ids", ()))
                if not set(cited) <= ids or not set(opposing) <= ids:
                    continue  # 跳过越界引用，不连坐其余合法 claim
                if not cited:
                    continue
                try:
                    claim = ClaimCard(trace_id=state["trace_id"], agent=self.name, symbol=state["symbol"],
                                      statement=str(item["statement"]), stance=item["stance"],
                                      confidence=float(item["confidence"]), supporting_evidence_ids=cited,
                                      contradicting_evidence_ids=opposing,
                                      upstream_card_ids=upstream,
                                      unknowns=tuple(item.get("unknowns", ())))
                except (KeyError, TypeError, ValueError):
                    continue  # 单张 schema 不合规则丢弃该张
                cards.append(claim)
            if not cards:
                return [], RouteDecision(node=self.name, route="abstain", reason_code=ReasonCode.SCHEMA_VIOLATION,
                                          detail="no valid claims survived citation boundary check")
        except (KeyError, TypeError, ValueError):
            return [], RouteDecision(node=self.name, route="abstain", reason_code=ReasonCode.SCHEMA_VIOLATION,
                                      detail="model output does not satisfy claims array schema")
        except Exception:
            return [], RouteDecision(node=self.name, route="failed", reason_code=ReasonCode.MODEL_FAILURE,
                                      detail="model call failed")
        for card in cards:
            self.ledger.append_claim(card)
        return cards, RouteDecision(node=self.name, route="proceed")
