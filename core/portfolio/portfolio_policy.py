"""PortfolioIntent 到可执行订单的唯一确定性入口。

职责：将上游研究链路产出的 PortfolioIntent（目标声明）经过去四层确定性转换：
  1. ConstraintBuilder  — 将意图转为带约束的向量 (位置/流动性/强制退出)
  2. RiskBudgetAllocator — 在波动率/协方差下求解相对权重
  3. VolatilityTarget   — 缩放至目标波动率并生成 TargetPortfolio
  4. CostGate           — 考虑交易成本和不可交易标的后输出 OrderPlan

每一阶段使用 policy_version 标记，保证审计回放时确定性与可追溯性。
"""
from collections.abc import Mapping, Set
from dataclasses import dataclass
from core.research.contracts import (
    IntentConstraints,
    OrderPlan,
    PortfolioIntent,
    RiskProjectedPortfolio,
    TargetPortfolio,
)
from .constraints import IntentConstraintBuilder
from .cost_gate import CostGate, CostLimits, CostModel, TradingInputs
from .policy import AllocationConfig, RiskBudgetAllocator, VolatilityTarget
from .risk_projection import RiskLimits, RiskProjection

@dataclass(frozen=True)
class PortfolioPolicyResult:
    """单次 build 调用的完整中间产物，便于下游逐段审计。"""

    constraints: IntentConstraints
    target: TargetPortfolio
    projected: RiskProjectedPortfolio
    order_plan: OrderPlan

class PortfolioPolicy:
    """组合政策执行器，将意图→目标→风控→订单的全流程编排为单一 build 方法。"""

    def __init__(
        self,
        *,
        constraint_builder: IntentConstraintBuilder,
        allocation: AllocationConfig,
        volatility_target: VolatilityTarget,
        risk_limits: RiskLimits,
        cost_limits: CostLimits,
        cost_model: CostModel,
        policy_version: str,
    ):
        # policy_version 必须非空，否则无法在日志中定位具体哪一版策略产生了该订单
        if not policy_version:
            raise ValueError("policy_version is required for audit replay")

        self.constraint_builder = constraint_builder
        self.allocation = allocation
        self.volatility_target = volatility_target
        self.risk_limits = risk_limits
        self.cost_limits = cost_limits
        self.cost_model = cost_model
        self.policy_version = policy_version

        self.allocator = RiskBudgetAllocator()
        self.projection = RiskProjection()
        self.cost_gate = CostGate()

    # --- 主入口 ---
    # 调用方须保证 current_weights/volatilities/covariance 为同一时点的 PIT 数据。
    # drawdown / risk_locked 由账户级风控传入，影响最终 target 是否被强制缩减。
    def build(
        self,
        intent: PortfolioIntent,
        *,
        trace_id: str,
        current_weights: Mapping[str, float],
        volatilities: Mapping[str, float],
        covariance: Mapping[str, Mapping[str, float]],
        trading: TradingInputs,
        drawdown: float,
        untradable: Set[str] = frozenset(),
        stop_loss: Set[str] = frozenset(),
        risk_locked: bool = False,
    ) -> PortfolioPolicyResult:
        # Step 1: 将 PortfolioIntent 解析为带方向/上下限/强制退出标记的约束向量
        constraints = self.constraint_builder.build(
            intent, current_weights, trace_id=trace_id,
        )
        # Step 2: 在协方差约束下分配相对权重；返回 sum<1 表示无可行完全投资解
        relative = self.allocator.allocate(
            constraints,
            volatilities=volatilities,
            covariance=covariance,
            current_weights=current_weights,
            config=self.allocation,
        )
        # 分配器返回和<1 的权重即「无可行完全投资域」信号：直接落保持不动目标，跳过波动率再缩放。
        if sum(relative.values()) < 1.0 - 1e-9:
            target = self.volatility_target.hold_target(
                intent, relative,
                trace_id=trace_id,
                policy_version=self.policy_version,
            )
        else:
            # Step 3: 正常路径——按目标波动率缩放，生成最终 TargetPortfolio
            target = self.volatility_target.build_target(
                intent, relative, covariance,
                current_exposure=sum(current_weights.values()),
                trace_id=trace_id,
                policy_version=self.policy_version,
            )
        # Step 4: 风控投影——检查目标组合是否违反限额（波动率/回撤/不可交易等），必要时缩减
        projected = self.projection.project(
            target,
            current_weights=current_weights,
            limits=self.risk_limits,
            covariance=covariance,
            drawdown=drawdown,
            untradable=set(untradable) | set(trading.untradable_symbols),
            stop_loss=stop_loss,
            risk_locked=risk_locked,
        )
        # 合并约束层 force_exit 与外部 stop_loss，统一传给 CostGate 标记为必须执行的减仓
        forced = {x.symbol for x in constraints.items if x.force_exit} | set(stop_loss)
        # Step 5: 成本门控——根据交易模型和费用限额生成最终可执行 OrderPlan
        order = self.cost_gate.build_order_plan(
            projected,
            current_weights=current_weights,
            inputs=trading,
            limits=self.cost_limits,
            model=self.cost_model,
            forced_exits=forced,
        )
        return PortfolioPolicyResult(constraints, target, projected, order)
