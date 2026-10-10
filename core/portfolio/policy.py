"""Plan.md 规定的受约束风险预算与波动率目标，不接受隐式市场假设。"""

from collections.abc import Mapping
from dataclasses import dataclass
from math import sqrt

import cvxpy as cp
import numpy as np

from core.research.contracts import IntentConstraints, PortfolioIntent, PortfolioWeight, TargetPortfolio


@dataclass(frozen=True)
class AllocationConfig:
    alpha: float
    beta: float
    gamma: float
    max_turnover: float
    max_sector_weight: float
    sectors: Mapping[str, str]
    liquidity_caps: Mapping[str, float]
    solver: str = "CLARABEL"

    def __post_init__(self) -> None:
        if min(self.alpha, self.beta, self.gamma) < 0 or not 0 < self.max_turnover <= 1 or not 0 < self.max_sector_weight <= 1:
            raise ValueError("invalid allocation configuration")


class RiskBudgetAllocator:
    """在 intent 边界内解一次受约束二次优化，产出「完全投资」（和=1）的相对权重；
    不做总暴露/波动率缩放（那是 VolatilityTarget 的事）。"""

    def allocate(self, constraints: IntentConstraints, *, volatilities: Mapping[str, float],
                 covariance: Mapping[str, Mapping[str, float]], current_weights: Mapping[str, float],
                 config: AllocationConfig) -> dict[str, float]:
        # 只保留上限非零的标的：exit、被 MAX_HOLDINGS 挡下的新 long（均 [0,0]）根本不进优化。
        items = [item for item in constraints.items if item.max_weight > 0.0]
        if not items:
            return {}
        symbols = [item.symbol for item in items]
        missing = [s for s in symbols if s not in volatilities or s not in covariance or s not in config.sectors or s not in config.liquidity_caps]
        if missing:
            raise ValueError(f"allocation inputs missing symbols: {sorted(missing)}")
        sigma = np.array([[float(covariance[a][b]) for b in symbols] for a in symbols], dtype=float)
        # 半正定校验：保证组合方差 >=0、优化问题凸；1e-9 容忍数值微负。
        if not np.isfinite(sigma).all() or np.linalg.eigvalsh((sigma + sigma.T) / 2).min() < -1e-9:
            raise ValueError("covariance must be finite positive semidefinite")
        vols = np.array([float(volatilities[s]) for s in symbols])
        if np.any(~np.isfinite(vols)) or np.any(vols <= 0):
            raise ValueError("volatilities must be finite and positive")
        # 逆波动率基准：1/vol 归一，alpha 项把解拉向它（防集中）。
        reference = (1.0 / vols) / np.sum(1.0 / vols)
        # previous = 上一轮各票权重（换手参照）；lower = intent 给的每票权重下界。
        previous = np.array([float(current_weights.get(s, 0.0)) for s in symbols])
        lower = np.array([item.min_weight for item in items])
        # ``trading_allowed=False`` 表示研究层明确冻结该标的（abstain 或未获准的
        # 新 long）。对已有的 abstain 持仓，不能先拿「新增配置上限」把固定权重截断，
        # 否则优化器会在没有风险原因码的情况下变更持仓，甚至在当前仓位高于上限时
        # 直接判为不可行。硬风险收缩只属于后续 RiskProjection。
        upper = np.array([
            item.max_weight if not item.trading_allowed
            else min(item.max_weight, float(config.liquidity_caps[symbol]))
            for item, symbol in zip(items, symbols, strict=True)
        ])
        # 边界自相矛盾或强制下限之和超 100% 是脏数据，必须显式拒绝。
        if np.any(lower > upper) or lower.sum() > 1.0 + 1e-9:
            raise ValueError("action bounds are contradictory or force an over-invested portfolio")
        # 天花板之和不足 100%：子集池 + 现金 + 全员观望时无法凑出「完全投资」相对组合。
        # 这不是脏数据而是本轮没有任何标的可加仓：退回「边界内夹住当前持仓」的绝对权重（和 < 1），
        # 交给 PortfolioPolicy.build 识别并跳过波动率再缩放，而不是让整轮崩溃。
        if upper.sum() < 1.0 - 1e-9:
            held = np.clip(previous, lower, upper)
            return {symbol: float(value) for symbol, value in zip(symbols, np.maximum(held, 0.0), strict=True)}
        weight = cp.Variable(len(symbols))
        # 目标(被最小化的式子；argmin 在 solve)：alpha·离基准² + beta·组合方差 w^TΣw + gamma·L1 换手。
        # (sigma+sigma.T)/2 强制对称化——二次型只认对称矩阵。
        objective = cp.Minimize(
            config.alpha * cp.sum_squares(weight - reference)
            + config.beta * cp.quad_form(weight, (sigma + sigma.T) / 2)
            + config.gamma * cp.norm1(weight - previous)
        )
        # 约束(可行域)：Σw=1 完全投资、lower<=w<=upper 尊重 intent 边界、L1 换手上限；下面追加逐行业上限。
        rules = [
            cp.sum(weight) == 1,
            weight >= lower,
            weight <= upper,
            cp.norm1(weight - previous) <= config.max_turnover,
        ]
        for sector in sorted(set(config.sectors.values())):
            idx = [i for i, symbol in enumerate(symbols) if config.sectors[symbol] == sector]
            rules.append(cp.sum(weight[idx]) <= config.max_sector_weight)
        problem = cp.Problem(objective, rules)      # 目标 + 约束装配成一道优化题
        problem.solve(solver=config.solver)          # 数值求解，argmin 在此发生
        # 非 OPTIMAL 视为约束无解 → 报错。
        if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE) or weight.value is None:
            raise ValueError(f"allocation problem is infeasible: {problem.status}")
        # weight.value 即解出的相对权重 w*(和≈1)；maximum 抹掉极小负权噪声。
        return {
            symbol: float(value)
            for symbol, value in zip(
                symbols,
                np.maximum(np.asarray(weight.value).reshape(-1), 0.0),
                strict=True,
            )
        }


class VolatilityTarget:
    """把分配器给出的相对权重按波动率目标换算为绝对暴露（定总仓大小）。"""

    def __init__(
        self,
        *,
        target_volatility: float,
        min_exposure: float,
        max_exposure: float,
        max_exposure_increase: float,
    ) -> None:
        if (
            not 0 < target_volatility
            or not 0 <= min_exposure <= max_exposure <= 1
            or not 0 <= max_exposure_increase <= 1
        ):
            raise ValueError("invalid volatility-target configuration")
        self.target_volatility = target_volatility
        self.min_exposure = min_exposure
        self.max_exposure = max_exposure
        self.max_exposure_increase = max_exposure_increase

    def build_target(
        self,
        intent: PortfolioIntent,
        relative_weights: Mapping[str, float],
        covariance: Mapping[str, Mapping[str, float]],
        *,
        current_exposure: float,
        trace_id: str,
        policy_version: str,
    ) -> TargetPortfolio:
        # 组合方差 = w^TΣw（满仓形状、每单位仓位的波动平方，与总仓无关）。
        symbols = tuple(sorted(relative_weights))
        vector = np.array([relative_weights[s] for s in symbols])
        matrix = np.array([[float(covariance[a][b]) for b in symbols] for a in symbols])
        variance = float(vector @ matrix @ vector)
        if variance <= 0 or not np.isfinite(variance):
            raise ValueError("portfolio variance must be finite and positive")
        # 目标仓位 = 目标波动/实际波动(√variance)，夹到 [min,max]：波动大就降杠杆。
        desired = min(self.max_exposure, max(self.min_exposure, self.target_volatility / sqrt(variance)))
        # 升仓限速：减仓随时允许，加仓每轮最多 +max_exposure_increase。
        exposure = desired if desired <= current_exposure else min(desired, current_exposure + self.max_exposure_increase)
        # 绝对权重 = 相对权重 × 目标仓位
        weights = tuple(
            PortfolioWeight(symbol=s, weight=float(relative_weights[s] * exposure))
            for s in symbols
        )
        return TargetPortfolio(
            trace_id=trace_id,
            intent_id=intent.intent_id,
            snapshot_id=intent.snapshot_id,
            policy_version=policy_version,
            weights=weights,
            total_exposure=sum(x.weight for x in weights),
            cash_weight=1.0 - sum(x.weight for x in weights),
        )

    def hold_target(
        self,
        intent: PortfolioIntent,
        weights: Mapping[str, float],
        *,
        trace_id: str,
        policy_version: str,
    ) -> TargetPortfolio:
        """把「边界内保持不动」的绝对权重直接落为 TargetPortfolio，绕开波动率再缩放。

        RiskBudgetAllocator 在无可行完全投资域时返回按当前持仓夹到边界内的绝对权重（和 < 1）；
        此时再乘 target_volatility/sqrt(variance) 会凭空调仓，违背「本轮不主动交易」语义。
        后续 RiskProjection 仍会按集中/部门/波动的硬约束只缩小权重，不会凭空加仓。
        """
        items = tuple(
            PortfolioWeight(symbol=s, weight=float(w))
            for s, w in sorted(weights.items()) if w > 0.0
        )
        exposure = sum(x.weight for x in items)
        return TargetPortfolio(
            trace_id=trace_id,
            intent_id=intent.intent_id,
            snapshot_id=intent.snapshot_id,
            policy_version=policy_version,
            weights=items,
            total_exposure=exposure,
            cash_weight=1.0 - exposure,
        )
