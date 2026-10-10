"""以 T 日开盘价执行已经批准的 OrderPlan。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import floor

from core.research.contracts import Fill, OrderPlan, PlannedOrder
from core.research.reasons import ReasonCode


@dataclass
class ExecutionAccount:
    cash: float
    holdings: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash < 0.0 or any(shares < 0 for shares in self.holdings.values()):
            raise ValueError("account cash and holdings must be non-negative")


@dataclass(frozen=True)
class ExecutionLimits:
    max_participation_rate: float
    slippage: float
    commission_per_order: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_participation_rate <= 1.0:
            raise ValueError("max_participation_rate must be in [0, 1]")
        if self.slippage < 0.0 or self.commission_per_order < 0.0:
            raise ValueError("execution costs must be non-negative")


@dataclass(frozen=True)
class ExecutionResult:
    account: ExecutionAccount
    fills: tuple[Fill, ...]


class SimulatedExecution:
    """不重新选择证券；只将目标权重在成交日开盘转换为实际成交。"""

    def execute(self, plan: OrderPlan, *, trace_id: str, decision_at: datetime,
                executed_at: datetime, account: ExecutionAccount,
                open_prices: Mapping[str, float], volumes: Mapping[str, float],
                limits: ExecutionLimits) -> ExecutionResult:
        if executed_at <= decision_at:
            raise ValueError("T-day execution must be after the T-1 decision")
        equity = account.cash
        for symbol, shares in account.holdings.items():
            price = open_prices.get(symbol)
            if price is None or price <= 0.0:
                raise ValueError(f"missing valid opening price for holding {symbol}")
            equity += shares * price

        def priority(order: PlannedOrder) -> tuple[int, str]:
            return (0 if order.forced and order.side == "sell" else 1 if order.side == "sell" else 2, order.symbol)

        fills: list[Fill] = []
        for order in sorted(plan.orders, key=priority):
            fills.append(self._execute_order(
                plan, order, trace_id, decision_at, executed_at, account, equity,
                open_prices, volumes, limits,
            ))
        return ExecutionResult(account=account, fills=tuple(fills))

    @staticmethod
    def _execute_order(plan: OrderPlan, order: PlannedOrder, trace_id: str,
                       decision_at: datetime, executed_at: datetime, account: ExecutionAccount,
                       equity: float, open_prices: Mapping[str, float], volumes: Mapping[str, float],
                       limits: ExecutionLimits) -> Fill:
        current = account.holdings.get(order.symbol, 0)
        price = open_prices.get(order.symbol)
        if price is None or price <= 0.0:
            return Fill(trace_id=trace_id, order_plan_id=plan.order_plan_id, symbol=order.symbol,
                        side=order.side, decision_at=decision_at, executed_at=executed_at,
                        requested_shares=0, filled_shares=0, executed_price=None, commission=0.0,
                        status="deferred", reason_codes=(ReasonCode.UNTRADABLE,))
        target = floor(equity * order.target_weight / price)
        delta = target - current
        if (order.side == "buy" and delta <= 0) or (order.side == "sell" and delta >= 0):
            return Fill(trace_id=trace_id, order_plan_id=plan.order_plan_id, symbol=order.symbol,
                        side=order.side, decision_at=decision_at, executed_at=executed_at,
                        requested_shares=0, filled_shares=0, executed_price=None, commission=0.0,
                        status="deferred", reason_codes=(ReasonCode.NO_TRADE_BAND,))
        requested = abs(delta)
        volume = volumes.get(order.symbol)
        if volume is None or volume < 0.0:
            return Fill(trace_id=trace_id, order_plan_id=plan.order_plan_id, symbol=order.symbol,
                        side=order.side, decision_at=decision_at, executed_at=executed_at,
                        requested_shares=requested, filled_shares=0, executed_price=None, commission=0.0,
                        status="deferred", reason_codes=(ReasonCode.UNTRADABLE,))
        permitted = min(requested, floor(volume * limits.max_participation_rate))
        limiting_reason = ReasonCode.LIQUIDITY_CAP
        if order.side == "sell":
            permitted = min(permitted, current)
            execution_price = price * (1.0 - limits.slippage)
            if permitted:
                account.holdings[order.symbol] = current - permitted
                if account.holdings[order.symbol] == 0:
                    del account.holdings[order.symbol]
                account.cash += permitted * execution_price - limits.commission_per_order
        else:
            execution_price = price * (1.0 + limits.slippage)
            affordable = floor(max(0.0, account.cash - limits.commission_per_order) / execution_price)
            if affordable < permitted:
                limiting_reason = ReasonCode.INSUFFICIENT_CASH
            permitted = min(permitted, affordable)
            if permitted:
                account.holdings[order.symbol] = current + permitted
                account.cash -= permitted * execution_price + limits.commission_per_order
        if permitted == requested:
            status, codes = "filled", ()
        elif permitted == 0:
            status, codes = "deferred", (limiting_reason,)
        else:
            status, codes = "partial", (limiting_reason,)
        return Fill(trace_id=trace_id, order_plan_id=plan.order_plan_id, symbol=order.symbol,
                    side=order.side, decision_at=decision_at, executed_at=executed_at,
                    requested_shares=requested, filled_shares=permitted,
                    executed_price=execution_price if permitted else None,
                    commission=limits.commission_per_order if permitted else 0.0,
                    status=status, reason_codes=codes)
