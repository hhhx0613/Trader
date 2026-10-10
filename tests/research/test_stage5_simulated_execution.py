"""阶段 5：冻结的订单只能在下一交易日开盘以可审计方式成交。"""

from datetime import datetime, timedelta, timezone

import pytest

from core.execution import ExecutionAccount, ExecutionLimits, SimulatedExecution
from core.research.contracts import OrderPlan, PlannedOrder
from core.research.reasons import ReasonCode


T1 = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)
T0 = T1 + timedelta(days=1)


def _plan(*orders: PlannedOrder) -> OrderPlan:
    return OrderPlan(trace_id="portfolio_trace", order_plan_id="op_1", projected_portfolio_id="rpp_1",
                     snapshot_id="snap_1", orders=orders)


def test_executes_forced_sell_before_buy_at_next_open():
    plan = _plan(
        PlannedOrder(symbol="AAPL", side="buy", target_weight=0.5),
        PlannedOrder(symbol="TSLA", side="sell", target_weight=0.0, forced=True),
    )
    result = SimulatedExecution().execute(
        plan, trace_id="execution_trace", decision_at=T1, executed_at=T0,
        account=ExecutionAccount(cash=0.0, holdings={"TSLA": 10}),
        open_prices={"AAPL": 10.0, "TSLA": 10.0}, volumes={"AAPL": 100, "TSLA": 100},
        limits=ExecutionLimits(max_participation_rate=1.0, slippage=0.0, commission_per_order=0.0),
    )
    assert [fill.symbol for fill in result.fills] == ["TSLA", "AAPL"]
    assert all(fill.status == "filled" for fill in result.fills)
    assert result.account.holdings == {"AAPL": 5}


def test_missing_open_and_participation_cap_are_explicit_not_silent():
    plan = _plan(
        PlannedOrder(symbol="AAPL", side="buy", target_weight=0.5),
        PlannedOrder(symbol="MSFT", side="buy", target_weight=0.5),
    )
    result = SimulatedExecution().execute(
        plan, trace_id="execution_trace", decision_at=T1, executed_at=T0,
        account=ExecutionAccount(cash=1000.0), open_prices={"AAPL": 10.0},
        volumes={"AAPL": 2}, limits=ExecutionLimits(0.5, 0.0, 0.0),
    )
    fills = {fill.symbol: fill for fill in result.fills}
    assert fills["AAPL"].status == "partial" and fills["AAPL"].reason_codes == (ReasonCode.LIQUIDITY_CAP,)
    assert fills["MSFT"].status == "deferred" and fills["MSFT"].reason_codes == (ReasonCode.UNTRADABLE,)


def test_rejects_same_day_execution():
    with pytest.raises(ValueError, match="after"):
        SimulatedExecution().execute(
            _plan(PlannedOrder(symbol="AAPL", side="buy", target_weight=0.5)),
            trace_id="execution_trace", decision_at=T1, executed_at=T1,
            account=ExecutionAccount(cash=100.0), open_prices={"AAPL": 10.0}, volumes={"AAPL": 100},
            limits=ExecutionLimits(1.0, 0.0, 0.0),
        )


def test_insufficient_cash_is_deferred_with_its_own_reason():
    result = SimulatedExecution().execute(
        _plan(PlannedOrder(symbol="AAPL", side="buy", target_weight=1.0)),
        trace_id="execution_trace", decision_at=T1, executed_at=T0,
        account=ExecutionAccount(cash=0.0, holdings={"TSLA": 10}),
        open_prices={"AAPL": 10.0, "TSLA": 10.0}, volumes={"AAPL": 100},
        limits=ExecutionLimits(1.0, 0.0, 0.0),
    )
    assert result.fills[0].status == "deferred"
    assert result.fills[0].reason_codes == (ReasonCode.INSUFFICIENT_CASH,)
