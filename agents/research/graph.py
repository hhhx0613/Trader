"""单标的 LangGraph 编排 / Per-asset LangGraph orchestration.

本模块只持有图拓扑、trace 生命周期和 ResearchPacket 收口。Event、Fundamental、
Market 与 Risk Critic 各自位于独立模块，从而让研究职责、prompt 和测试可单独演进。

流程图（节点名对应 _build_graph 的 add_node，括号内为写入 ResearchState 的键）：

                         输入：{snapshot_id, symbol, as_of, trace_id}
                                        |
                                        v
START --> [event]  EventAgent：读新闻/公告立论或弃权
                    （event_claims, event_route）
                          |
            --------------+-----------------
            | 并行扇出（同一 superstep）    |
            v                              v
      [fundamental]                  [market]
      仅在 event_route=proceed       读 Event 论点作只读语境，
      时产出，否则自行跳过            但引用锁在行情类，不被 Event 连坐
      （fundamental_claims）         （market_claims）
            |                              |
            --------------+----------------
                          v 扇入
                    [critic]  RiskCritic：软否决审计三叠 ClaimCard
                              （critic：allow/caution/abstain/human_review）
                          |
                          v
                    [packet]  程序收口：汇总 Card ID、引用覆盖率、
                              Critic 结论与持仓语境 -> ResearchPacket 落账本
                          |     （packet）
                          v
                         END

路由语义：弃权/缺证据不中断图——各节点降级写入，[packet] 永远产出合法
Packet；否决的执行在委员会层（committee._item_violation），不在本图内。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from core.research import DataGateway, ResearchLedger, ResearchPacket
from core.research.contracts import CriticVerdict

from .event_agent import EventAgent
from .fundamental_agent import FundamentalAgent
from .market_agent import MarketAgent
from .risk_critic import RiskCritic
from .types import JsonModel, ResearchState


class PerAssetResearchGraph:
    """为一个已冻结标的运行无状态研究图 / Run one isolated research graph per symbol."""

    def __init__(self, *, ledger: ResearchLedger, gateway: DataGateway, model: JsonModel) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self._event_agent = EventAgent(ledger=ledger, gateway=gateway, model=model)
        self._fundamental_agent = FundamentalAgent(ledger=ledger, gateway=gateway, model=model)
        self._market_agent = MarketAgent(ledger=ledger, gateway=gateway, model=model)
        self._risk_critic = RiskCritic(gateway=gateway, model=model)
        self._graph = self._build_graph()

    def run(self, *, snapshot_id: str, symbol: str, as_of: datetime,
            trace_id: str | None = None) -> ResearchPacket:
        """运行时新建 research trace，绝不向已完成的 Snapshot trace 追写判断。"""
        normalized_symbol = symbol.strip().upper()
        # 输入边界在此校验一次；图内所有读取都固定在这份 Snapshot 与 as_of 上，不联网。
        self.gateway.snapshot_metadata(snapshot_id=snapshot_id, symbol=normalized_symbol, as_of=as_of)
        trace_id = trace_id or f"research_{uuid4().hex}"
        self.ledger.start_trace(trace_id, as_of)
        try:
            state = self._graph.invoke({"snapshot_id": snapshot_id, "symbol": normalized_symbol,
                                        "as_of": as_of, "trace_id": trace_id})
            packet = state["packet"]
            self.ledger.complete_trace(trace_id, as_of)
            return packet
        except Exception:
            # 保留 running trace 比伪造成功记录更诚实；调用者可据此审计中断原因。
            raise

    def _build_graph(self):
        graph = StateGraph(ResearchState)
        graph.add_node("event", self._event_agent)
        graph.add_node("fundamental", self._fundamental_agent)
        graph.add_node("market", self._market_agent)
        graph.add_node("critic", self._risk_critic)
        graph.add_node("packet", self._packet)
        graph.add_edge(START, "event")
        # Event 立论后并发扇出：Fundamental 用独立披露核验事件论点；Market 以事件论点为语境判价量确认/背离，
        # 但补查与引用锁在行情维度，不被 Event 弃权连坐（见 market_agent）。
        graph.add_edge("event", "fundamental")
        graph.add_edge("event", "market")
        graph.add_edge("fundamental", "critic")
        graph.add_edge("market", "critic")
        graph.add_edge("critic", "packet")
        graph.add_edge("packet", END)
        return graph.compile()

    def _packet(self, state: ResearchState) -> dict[str, Any]:
        claims: list = []
        for key in ("event_claims", "fundamental_claims", "market_claims"):
            claims.extend(state.get(key) or [])
        critic = state.get("critic") or CriticVerdict(verdict="abstain", reasons=["critic absent from graph state"])
        # Event 未通过时只跑了 1 个 Agent；通过时三个 Agent 都可能出卡，覆盖率须按实际参与的 Agent 计。
        eligible = 3 if state.get("event_route") and state["event_route"].route == "proceed" else 1
        active_agents = sum(1 for k in ("event_claims", "fundamental_claims", "market_claims") if state.get(k))
        coverage = active_agents / eligible if eligible else 0.0
        packet = ResearchPacket(trace_id=state["trace_id"], symbol=state["symbol"], snapshot_id=state["snapshot_id"],
                                claim_card_ids=tuple(item.claim_id for item in claims), citation_coverage=coverage,
                                critic=critic, position_context=self.gateway.get_current_portfolio(
                                    snapshot_id=state["snapshot_id"], symbol=state["symbol"], as_of=state["as_of"]))
        self.ledger.append_packet(packet)
        return {"packet": packet}
