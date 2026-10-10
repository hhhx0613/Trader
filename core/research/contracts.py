"""阶段一可审计研究链路的数据契约 / Versioned contracts for audited research.

这里定义写入账本或跨节点传递的不可变 Pydantic 对象。每个对象携带 schema
版本、trace 与创建时间，确保离线回放可辨识其数据格式和来源。它们描述事实
及其引用，不承载模型判断、权重或订单。

This module defines immutable Pydantic objects persisted in the ledger or passed
between nodes. Schema version, trace, and creation time make offline replay
format-aware and attributable. Contracts represent facts and references, never
model judgments, portfolio weights, or orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .reasons import ReasonCode


SCHEMA_VERSION = "1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    """阶段一持久化对象的基础字段 / Common fields for persisted Stage-1 objects.

    ``extra="forbid"`` 是契约边界的一部分：Committee 对象试图携带权重或订单
    字段时，必须在写入账本前被确定拒绝，而不是靠约定。

    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    trace_id: str
    created_at: datetime = Field(default_factory=utc_now)


class RawReference(BaseModel):
    """原始内容指针 / Pointer to content-addressed raw input.

    SQLite 只保存路径、哈希和来源，避免把大文本复制进账本；读取方可据此验证
    文件未被篡改。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 字段名为历史包留（旧账本行已序列化为文件路径）；新语义是指针 <kind>://<sha256>，
    # 正文存在按源分库的 payload 库（news/market/filings.db），多 trace 共享一份字节。
    relative_path: str
    source: str
    received_at: datetime


class EvidenceCard(Contract):
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex}")
    kind: Literal["news", "filing", "market"]
    symbol: str
    published_at: datetime | None = None
    available_at: datetime
    source: str
    raw: RawReference
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_url: str | None = None
    duplicate_of: str | None = None
    rejection_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be empty")
        return value


class ResearchSnapshot(Contract):
    snapshot_id: str = Field(default_factory=lambda: f"snap_{uuid4().hex}")
    as_of: datetime
    symbols: tuple[str, ...]
    source_versions: dict[str, str]
    evidence_ids: tuple[str, ...]
    account_state: dict[str, Any]

    @field_validator("symbols")
    @classmethod
    def unique_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(symbol.strip().upper() for symbol in value)
        if not normalized or any(not symbol for symbol in normalized):
            raise ValueError("snapshot requires at least one non-empty symbol")
        if len(set(normalized)) != len(normalized):
            raise ValueError("snapshot symbols must be unique")
        return normalized


IntentAction = Literal["long", "hold", "reduce", "exit", "abstain"]
NodeRoute = Literal["proceed", "abstain", "data_request", "human_review", "no_trade", "failed"]


class ValueObject(BaseModel):
    """嵌入持久化契约的冻结值对象 / Frozen embedded value objects, never persisted alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RouteDecision(ValueObject):
    """图节点的显式路由 / Explicit graph-node routing.

    除 ``proceed`` 外的每条路由必须携带原因码，保证失败分支可审计。
    """

    node: str = Field(min_length=1)
    route: NodeRoute
    reason_code: ReasonCode | None = None
    detail: str = ""

    @model_validator(mode="after")
    def require_reason_for_non_proceed(self) -> "RouteDecision":
        if self.route != "proceed" and self.reason_code is None:
            raise ValueError("non-proceed route requires a reason_code")
        return self


class CriticVerdict(ValueObject):
    """Risk Critic 的研究软否决结论 / Soft research veto from the Risk Critic."""

    verdict: Literal["allow", "caution", "abstain", "human_review"]
    reasons: tuple[str, ...] = ()
    card_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_reasons(self) -> "CriticVerdict":
        if not self.reasons:
            raise ValueError("critic verdict requires reasons")
        return self


class ClaimCard(Contract):
    """Agent 的可审计研究结论 / Cited research claim from one agent.

    引用约束在契约层强制：任何非观望的 Card 至少引用一条支持性 EvidenceCard，
    否则不允许进入下游对象。
    """

    claim_id: str = Field(default_factory=lambda: f"cl_{uuid4().hex}")
    agent: Literal["event", "fundamental", "market"]
    symbol: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    stance: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    upstream_card_ids: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def require_citation(self) -> "ClaimCard":
        if not self.supporting_evidence_ids:
            raise ValueError("claim requires at least one supporting EvidenceCard")
        return self


class ResearchPacket(Contract):
    """单标的研究汇总 / Per-symbol research bundle for the Committee."""

    packet_id: str = Field(default_factory=lambda: f"rp_{uuid4().hex}")
    symbol: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    claim_card_ids: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    citation_coverage: float = Field(ge=0.0, le=1.0)
    critic: CriticVerdict
    position_context: dict[str, Any] = Field(default_factory=dict)


class ThesisBook(Contract):
    """全池 Packet 摘要与调仓差异 / Pool-wide packet digests plus rebalancing deltas.

    Committee 只看这里的内容；原始全文与 Agent 对话不进入该对象。
    """

    thesis_book_id: str = Field(default_factory=lambda: f"tb_{uuid4().hex}")
    snapshot_id: str = Field(min_length=1)
    packet_ids: tuple[str, ...] = Field(min_length=1)
    portfolio_state: dict[str, Any]
    regime: str | None = None
    deltas: dict[str, Any] = Field(default_factory=dict)
    memory_case_ids: tuple[str, ...] = ()


class IntentItem(ValueObject):
    symbol: str = Field(min_length=1)
    action: IntentAction
    strength: int = Field(ge=1, le=3)
    priority: int = Field(ge=1)
    horizon_days: int | None = Field(default=None, ge=1)
    supporting_card_ids: tuple[str, ...] = ()
    opposing_card_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1)
    no_trade_reason: str | None = None


class PortfolioIntent(Contract):
    """LLM 研究到组合层的唯一边界 / The only LLM-to-portfolio boundary.

    构造上不存在权重、预期收益或订单字段；任何此类键都会被 extra="forbid" 拒绝。
    """

    intent_id: str = Field(default_factory=lambda: f"pi_{uuid4().hex}")
    snapshot_id: str = Field(min_length=1)
    items: tuple[IntentItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_symbols(self) -> "PortfolioIntent":
        symbols = [item.symbol for item in self.items]
        if len(set(symbols)) != len(symbols):
            raise ValueError("intent items must use unique symbols")
        return self


# 阶段 4 的对象仍放在同一契约模块：它们是研究、组合、执行之间的审计边界，
# 不能由某个具体算法模块私自定义成不受版本和 extra 校验保护的 dict。
class WeightConstraint(ValueObject):
    symbol: str = Field(min_length=1)
    min_weight: float = Field(ge=0.0, le=1.0)
    max_weight: float = Field(ge=0.0, le=1.0)
    trading_allowed: bool
    force_exit: bool = False
    reason_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def validate_bounds(self) -> "WeightConstraint":
        if self.min_weight > self.max_weight:
            raise ValueError("min_weight must not exceed max_weight")
        if self.force_exit and (self.min_weight != 0.0 or self.max_weight != 0.0):
            raise ValueError("force_exit requires a zero weight bound")
        return self


class IntentConstraints(Contract):
    intent_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    items: tuple[WeightConstraint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_symbols(self) -> "IntentConstraints":
        if len({item.symbol for item in self.items}) != len(self.items):
            raise ValueError("constraint items must use unique symbols")
        return self


class PortfolioWeight(ValueObject):
    symbol: str = Field(min_length=1)
    weight: float = Field(ge=0.0, le=1.0)


class TargetPortfolio(Contract):
    target_portfolio_id: str = Field(default_factory=lambda: f"tp_{uuid4().hex}")
    intent_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    weights: tuple[PortfolioWeight, ...] = ()
    cash_weight: float = Field(ge=0.0, le=1.0)
    total_exposure: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights(self) -> "TargetPortfolio":
        if len({item.symbol for item in self.weights}) != len(self.weights):
            raise ValueError("target weights must use unique symbols")
        if abs(sum(item.weight for item in self.weights) - self.total_exposure) > 1e-8:
            raise ValueError("target weights must sum to total_exposure")
        if abs(self.cash_weight + self.total_exposure - 1.0) > 1e-8:
            raise ValueError("cash_weight and total_exposure must sum to one")
        return self


class RiskAdjustment(ValueObject):
    symbol: str = Field(min_length=1)
    reason_code: ReasonCode
    from_weight: float = Field(ge=0.0, le=1.0)
    to_weight: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def only_shrinks_risk(self) -> "RiskAdjustment":
        if self.to_weight > self.from_weight:
            raise ValueError("risk adjustment must not increase a weight")
        return self


class RiskProjectedPortfolio(Contract):
    projected_portfolio_id: str = Field(default_factory=lambda: f"rpp_{uuid4().hex}")
    target_portfolio_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    weights: tuple[PortfolioWeight, ...] = ()
    cash_weight: float = Field(ge=0.0, le=1.0)
    total_exposure: float = Field(ge=0.0, le=1.0)
    adjustments: tuple[RiskAdjustment, ...] = ()

    @model_validator(mode="after")
    def validate_weights(self) -> "RiskProjectedPortfolio":
        if abs(sum(item.weight for item in self.weights) - self.total_exposure) > 1e-8:
            raise ValueError("projected weights must sum to total_exposure")
        if abs(self.cash_weight + self.total_exposure - 1.0) > 1e-8:
            raise ValueError("cash_weight and total_exposure must sum to one")
        return self


class PlannedOrder(ValueObject):
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    target_weight: float = Field(ge=0.0, le=1.0)
    forced: bool = False
    reason_codes: tuple[ReasonCode, ...] = ()


class DeferredTrade(ValueObject):
    symbol: str = Field(min_length=1)
    target_weight: float = Field(ge=0.0, le=1.0)
    reason_code: ReasonCode


class OrderPlan(Contract):
    order_plan_id: str = Field(default_factory=lambda: f"op_{uuid4().hex}")
    projected_portfolio_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    orders: tuple[PlannedOrder, ...] = ()
    deferred_trades: tuple[DeferredTrade, ...] = ()

    @model_validator(mode="after")
    def unique_order_symbols(self) -> "OrderPlan":
        if len({item.symbol for item in self.orders}) != len(self.orders):
            raise ValueError("orders must use unique symbols")
        return self


class Fill(Contract):
    """一次计划订单在 T 日开盘的实际成交，不把未成交伪装成零成本成交。"""

    fill_id: str = Field(default_factory=lambda: f"fill_{uuid4().hex}")
    order_plan_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    decision_at: datetime
    executed_at: datetime
    requested_shares: int = Field(ge=0)
    filled_shares: int = Field(ge=0)
    executed_price: float | None = Field(default=None, gt=0.0)
    commission: float = Field(ge=0.0)
    status: Literal["filled", "partial", "deferred"]
    reason_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def validate_execution(self) -> "Fill":
        if self.executed_at <= self.decision_at:
            raise ValueError("execution must occur after the decision")
        if self.status == "filled" and (self.filled_shares != self.requested_shares or self.filled_shares == 0):
            raise ValueError("filled status requires all requested shares")
        if self.status == "partial" and not (0 < self.filled_shares < self.requested_shares):
            raise ValueError("partial status requires a strict partial fill")
        if self.status == "deferred" and (self.filled_shares != 0 or self.executed_price is not None):
            raise ValueError("deferred status must not carry an execution")
        if self.status != "filled" and not self.reason_codes:
            raise ValueError("non-filled status requires a reason code")
        return self
