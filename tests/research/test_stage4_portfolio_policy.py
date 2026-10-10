"""阶段 4：每一项 Plan 约束必须进入求解、投影或成本门控。"""
import pytest
from core.portfolio import (AllocationConfig, CostLimits, CostModel, IntentConstraintBuilder, PortfolioPolicy,
                            RiskLimits, TradingInputs, VolatilityTarget)
from core.research.contracts import IntentItem, PortfolioIntent

def intent():
    return PortfolioIntent(trace_id="committee",snapshot_id="snap",items=(
        IntentItem(symbol="AAPL",action="hold",strength=1,priority=1,rationale="hold"),
        IntentItem(symbol="MSFT",action="long",strength=1,priority=1,rationale="long"),
        IntentItem(symbol="NVDA",action="exit",strength=1,priority=2,rationale="exit"),))

def policy():
    sectors={"AAPL":"tech","MSFT":"tech","NVDA":"tech"}
    return PortfolioPolicy(
        constraint_builder=IntentConstraintBuilder(max_position_weight=.8,hold_band=.05,seed_weight=.05,max_holdings=3),
        allocation=AllocationConfig(alpha=1,beta=.1,gamma=.1,max_turnover=1,max_sector_weight=1,sectors=sectors,liquidity_caps={s:.8 for s in sectors}),
        volatility_target=VolatilityTarget(target_volatility=.2,min_exposure=.1,max_exposure=.9,max_exposure_increase=.5),
        risk_limits=RiskLimits(max_exposure=.9,cash_floor=.1,max_position_weight=.8,max_sector_weight=1,max_turnover=1,max_portfolio_volatility=.4,drawdown_lock=.2,sectors=sectors),
        cost_limits=CostLimits(min_order_weight=.001,no_trade_band=0,max_turnover=1,max_participation_rate=.1,max_cost_rate=.1),
        cost_model=CostModel(commission_per_order=1,half_spread_bps=2,impact_coefficient=.01),policy_version="test-policy-v1")

def market():
    symbols=("AAPL","MSFT")
    covariance={a:{b:.04 if a==b else .01 for b in symbols} for a in symbols}
    return covariance,{"AAPL":.2,"MSFT":.2},TradingInputs(account_value=100000,cash=50000,prices={"AAPL":100,"MSFT":100,"NVDA":100},volumes={"AAPL":1e6,"MSFT":1e6,"NVDA":1e6},adv_notional={"AAPL":1e8,"MSFT":1e8,"NVDA":1e8})

def test_policy_uses_constrained_optimizer_and_preserves_exit():
    covariance,volatilities,trading=market()
    out=policy().build(intent(),trace_id="portfolio",current_weights={"AAPL":.4,"NVDA":.2},volatilities=volatilities,covariance=covariance,trading=trading,drawdown=0)
    constraints={x.symbol:x for x in out.constraints.items}
    assert constraints["MSFT"].min_weight==.05 and constraints["NVDA"].force_exit
    assert out.target.total_exposure == pytest.approx(.9)
    assert any(order.symbol=="NVDA" and order.forced for order in out.order_plan.orders)

def test_missing_market_or_allocation_input_fails_closed():
    covariance,volatilities,trading=market()
    with pytest.raises(ValueError,match="missing"):
        policy().build(intent(),trace_id="portfolio",current_weights={"AAPL":.4},volatilities={"AAPL":.2},covariance=covariance,trading=trading,drawdown=0)


def test_abstain_locks_target_until_hard_risk_projection_changes_it():
    covariance, volatilities, trading = market()
    abstaining = PortfolioIntent(trace_id="committee", snapshot_id="snap", items=(
        IntentItem(symbol="AAPL", action="abstain", strength=1, priority=1,
                   rationale="evidence insufficient", no_trade_reason="critic abstained"),
        IntentItem(symbol="MSFT", action="hold", strength=1, priority=2, rationale="hold"),
    ))
    out = policy().build(abstaining, trace_id="portfolio", current_weights={"AAPL": .4, "MSFT": .2},
                         volatilities=volatilities, covariance=covariance, trading=trading, drawdown=0)
    constraints = {item.symbol: item for item in out.constraints.items}
    assert constraints["AAPL"].trading_allowed is False
    assert constraints["AAPL"].min_weight == constraints["AAPL"].max_weight == pytest.approx(.4)
    assert constraints["AAPL"].reason_codes
    target = {item.symbol: item.weight for item in out.target.weights}
    assert target["AAPL"] == pytest.approx(.4)


def test_abstain_position_above_new_allocation_cap_reaches_risk_projection():
    covariance, volatilities, trading = market()
    frozen = PortfolioIntent(trace_id="committee", snapshot_id="snap", items=(
        IntentItem(symbol="AAPL", action="abstain", strength=1, priority=1,
                   rationale="evidence insufficient", no_trade_reason="critic abstained"),
        IntentItem(symbol="MSFT", action="hold", strength=1, priority=2, rationale="hold"),
    ))
    constrained = PortfolioPolicy(
        constraint_builder=IntentConstraintBuilder(max_position_weight=.8, hold_band=.05, seed_weight=.05, max_holdings=3),
        allocation=AllocationConfig(alpha=1, beta=.1, gamma=.1, max_turnover=1, max_sector_weight=1,
                                    sectors={"AAPL": "tech", "MSFT": "tech"}, liquidity_caps={"AAPL": .35, "MSFT": .8}),
        volatility_target=VolatilityTarget(target_volatility=.2, min_exposure=.1, max_exposure=.9, max_exposure_increase=.5),
        risk_limits=RiskLimits(max_exposure=.9, cash_floor=.1, max_position_weight=.8, max_sector_weight=1,
                               max_turnover=1, max_portfolio_volatility=.4, drawdown_lock=.2,
                               sectors={"AAPL": "tech", "MSFT": "tech"}),
        cost_limits=CostLimits(min_order_weight=.001, no_trade_band=0, max_turnover=1, max_participation_rate=.1, max_cost_rate=.1),
        cost_model=CostModel(commission_per_order=1, half_spread_bps=2, impact_coefficient=.01), policy_version="test-policy-v1")
    out = constrained.build(frozen, trace_id="portfolio", current_weights={"AAPL": .5, "MSFT": .2},
                            volatilities=volatilities, covariance=covariance, trading=trading, drawdown=0)
    target = {item.symbol: item.weight for item in out.target.weights}
    assert target["AAPL"] == pytest.approx(.5)
