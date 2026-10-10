"""订单级成本/流动性门控，强制退出仅能拆分或排队。"""
from collections.abc import Mapping, Set
from dataclasses import dataclass
from math import sqrt

from core.research.contracts import DeferredTrade, OrderPlan, PlannedOrder, RiskProjectedPortfolio
from core.research.reasons import ReasonCode


@dataclass(frozen=True)
class CostModel:
    """单笔成本的三个来源：固定佣金 + 半点差（买卖价差的一半）+ 冲击系数。"""

    commission_per_order: float
    half_spread_bps: float
    impact_coefficient: float

    def __post_init__(self):
        if min(self.commission_per_order, self.half_spread_bps, self.impact_coefficient) < 0:
            raise ValueError("cost model must be non-negative")


@dataclass(frozen=True)
class CostLimits:
    """五道闸门（均 [0,1] 比率）：最小下单权重 / 免动带 / 换手上限 / 单日参与率 / 单笔成本率。"""

    min_order_weight: float
    no_trade_band: float
    max_turnover: float
    max_participation_rate: float
    max_cost_rate: float

    def __post_init__(self):
        values = (
            self.min_order_weight,
            self.no_trade_band,
            self.max_turnover,
            self.max_participation_rate,
            self.max_cost_rate,
        )
        if not all(0 <= x <= 1 for x in values):
            raise ValueError("cost limits must be ratios")


@dataclass(frozen=True)
class TradingInputs:
    """撮合现场的账户与行情快照：总市值/现金/现价/当日量/日均成交额 ADV。"""

    account_value: float
    cash: float
    prices: Mapping[str, float]
    volumes: Mapping[str, float]
    adv_notional: Mapping[str, float]
    untradable_symbols: Set[str] = frozenset()

    def __post_init__(self):
        if self.account_value <= 0 or self.cash < 0:
            raise ValueError("account value must be positive and cash non-negative")

class CostGate:
    """将风控投影后的目标组合转化为可执行订单计划，经过优先级排序和多道闸门筛选。"""

    def build_order_plan(
        self,
        projected: RiskProjectedPortfolio,
        *,
        current_weights: Mapping[str, float],
        inputs: TradingInputs,
        limits: CostLimits,
        model: CostModel,
        forced_exits: Set[str] = frozenset(),
    ) -> OrderPlan:
        # targets：投影后的目标权重；remaining_turnover/cash 是逐单递减的两本账
        targets = {x.symbol: x.weight for x in projected.weights}
        orders = []
        deferred = []
        remaining_turnover = limits.max_turnover
        cash = inputs.cash

        candidates = []
        # 第一遍：算每票目标与现仓的差额 delta，过滤掉「无需动」的
        for symbol in sorted(set(targets) | set(current_weights)):
            target = targets.get(symbol, 0.0)
            delta = target - current_weights.get(symbol, 0.0)
            forced = symbol in forced_exits
            if not delta:
                continue  # 目标=现仓，本就无需下单
            # 免动带/最小单：非强制且差额太小 → 挂起不交易
            if not forced and abs(delta) <= limits.no_trade_band:
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target,
                    reason_code=ReasonCode.NO_TRADE_BAND,
                ))
                continue
            if not forced and abs(delta) < limits.min_order_weight:
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target,
                    reason_code=ReasonCode.MIN_ORDER_SIZE,
                ))
                continue
            # 优先级键：强制卖(0) → 主动卖(1) → 买(2)；先卖后买既释放现金、又降风险
            priority = 0 if forced and delta < 0 else 1 if delta < 0 else 2
            candidates.append((priority, symbol, target, delta, forced))

        # 第二遍：按优先级顺序逐单过闸门
        for _, symbol, target, delta, forced in sorted(candidates):
            # 缺价/缺量/缺 adv 或非正 → 无法定价与估冲击，直接挂起
            if (
                symbol not in inputs.prices
                or symbol not in inputs.volumes
                or symbol not in inputs.adv_notional
                or inputs.prices[symbol] <= 0
                or inputs.adv_notional[symbol] <= 0
            ):
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target,
                    reason_code=ReasonCode.DEFERRED_LIQUIDITY,
                ))
                continue

            # 参与率闸门：单日最多成交该票当日量的 max_participation_rate
            desired = abs(delta)
            executable = min(
                desired,
                limits.max_participation_rate * inputs.volumes[symbol] * inputs.prices[symbol] / inputs.account_value,
            )
            # 换手预算只约束主动单；强制卖/买不受 remaining_turnover 封顶
            if not forced:
                executable = min(executable, remaining_turnover)

            # 成本 = 固定佣金 + 名义×(半点差 + 平方根冲击)
            notional = executable * inputs.account_value
            cost = (
                model.commission_per_order
                + notional * (
                    model.half_spread_bps / 10000
                    + model.impact_coefficient * sqrt(notional / inputs.adv_notional[symbol])
                )
            )

            # 买单受现金硬约束：名义+成本超现金则压缩可成交量
            if delta > 0 and notional + cost > cash:
                executable = max(0.0, (cash - model.commission_per_order) / inputs.account_value)
                notional = executable * inputs.account_value

            if executable <= 0:
                reason = ReasonCode.INSUFFICIENT_CASH if delta > 0 else ReasonCode.DEFERRED_LIQUIDITY
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target, reason_code=reason,
                ))
                continue

            # 成本率闸门：单笔成本占名义过高则不值得动（强制退出无视）
            if not forced and cost / notional > limits.max_cost_rate:
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target,
                    reason_code=ReasonCode.DEFERRED_COST,
                ))
                continue

            # 通过全部闸门：按实际可成交量落单
            final_target = current_weights.get(symbol, 0.0) + (
                executable if delta > 0 else -executable
            )
            orders.append(PlannedOrder(
                symbol=symbol,
                side="buy" if delta > 0 else "sell",
                target_weight=final_target,
                forced=forced,
                reason_codes=(),
            ))

            # 成交后更新两本账
            if delta > 0:
                cash -= notional + cost
            else:
                cash += notional - cost
            if not forced:
                remaining_turnover -= executable

            # 被截断的部分挂起到下一轮
            if executable < desired:
                deferred.append(DeferredTrade(
                    symbol=symbol, target_weight=target,
                    reason_code=ReasonCode.DEFERRED_LIQUIDITY,
                ))

        return OrderPlan(
            trace_id=projected.trace_id,
            projected_portfolio_id=projected.projected_portfolio_id,
            snapshot_id=projected.snapshot_id,
            orders=tuple(orders),
            deferred_trades=tuple(deferred),
        )
