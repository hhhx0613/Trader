"""研究节点共享类型 / Shared types for isolated research nodes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol, TypedDict

from core.research import ClaimCard, ResearchPacket
from core.research.contracts import CriticVerdict, RouteDecision


class JsonModel(Protocol):
    """节点唯一模型能力；生产端传入 LLMClient，测试端可注入无网络 fake。"""

    def chat_json(self, message: str, *, system_prompt: str | None = None,
                  temperature: float = 0.1, max_tokens: int | None = None,
                  max_retries: int = 1) -> dict[str, Any]: ...


class ToolCallingModel(JsonModel, Protocol):
    """生产模型的受控 function-calling 能力；工具实际由 harness 执行。"""

    def chat_with_tools(self, message: str, *, system_prompt: str | None,
                        tools: list[dict[str, Any]], tool_executor: Callable[[str, dict[str, Any]], str],
                        max_tool_calls: int, temperature: float = 0.0,
                        max_tokens: int | None = None) -> dict[str, Any]: ...


class ResearchState(TypedDict, total=False):
    """图状态只保存契约与标识，不保存原始正文、句柄或聊天历史。"""

    snapshot_id: str
    symbol: str
    as_of: datetime
    trace_id: str
    event_claims: list[ClaimCard]
    fundamental_claims: list[ClaimCard]
    market_claims: list[ClaimCard]
    critic: CriticVerdict
    event_route: RouteDecision
    fundamental_route: RouteDecision
    market_route: RouteDecision
    packet: ResearchPacket
