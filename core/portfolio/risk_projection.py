"""按 Plan 顺序施加硬约束；所有操作都只缩小目标风险。"""
from collections.abc import Mapping, Set
from dataclasses import dataclass
from math import sqrt

import numpy as np

from core.research.contracts import (
    PortfolioWeight,
    RiskAdjustment,
    RiskProjectedPortfolio,
    TargetPortfolio,
)
from core.research.reasons import ReasonCode


@dataclass(frozen=True)
class RiskLimits:
    """风控限额参数（均为 [0,1] 比率，除 sectors 是 symbol→行业映射）。"""

    max_exposure: float
    cash_floor: float
    max_position_weight: float
    max_sector_weight: float
    max_turnover: float
    max_portfolio_volatility: float
    drawdown_lock: float
    sectors: Mapping[str, str]

    def __post_init__(self):
        ratios = (
            self.max_exposure,
            self.cash_floor,
            self.max_position_weight,
            self.max_sector_weight,
            self.max_turnover,
            self.max_portfolio_volatility,
            self.drawdown_lock,
        )
        if not all(0 <= x <= 1 for x in ratios):
            raise ValueError("risk limits must be ratios in [0, 1]")

class RiskProjection:
    """对 TargetPortfolio 按固定顺序施加硬风控；只向下改权，每刀带原因码供审计。"""

    def project(
        self,
        target: TargetPortfolio,
        *,
        current_weights: Mapping[str, float],
        limits: RiskLimits,
        covariance: Mapping[str, Mapping[str, float]],
        drawdown: float,
        untradable: Set[str] = frozenset(),
        stop_loss: Set[str] = frozenset(),
        risk_locked: bool = False,
    ) -> RiskProjectedPortfolio:
        # weights：目标权重摊成 dict 就地改；changes：每次收缩记一条审计流水
        weights = {x.symbol: x.weight for x in target.weights}
        changes = []

        def shrink(symbol, to, code):
            """唯一改权入口：max(0,min(old,to)) 保证只减不增。"""
            old = weights.get(symbol, 0.0)
            to = max(0.0, min(old, to))
            if to < old - 1e-12:
                weights[symbol] = to
                changes.append(RiskAdjustment(
                    symbol=symbol, reason_code=code,
                    from_weight=old, to_weight=to,
                ))
        # 【执行顺序=Plan 顺序：身份类硬规则 → 结构类上限 → 换手；全程只缩小】
        # 1) 不可交易标的：目标压回当前持仓，只禁止加仓（不强制卖出）
        for s in sorted(untradable):
            shrink(s, float(current_weights.get(s, 0.0)), ReasonCode.UNTRADABLE)
        # 2) 触发止损：目标归零 = 强制清仓去风险
        for s in sorted(stop_loss):
            shrink(s, 0.0, ReasonCode.STOP_LOSS)
        # 3) 风控锁 / 回撤越锁：整个组合钉回当前持仓
        if risk_locked or drawdown >= limits.drawdown_lock:
            code = ReasonCode.RISK_STATE_LOCK if risk_locked else ReasonCode.DRAWDOWN_LOCK
            for s in tuple(weights):
                shrink(s, float(current_weights.get(s, 0.0)), code)
        # 4) 单票集中度上限
        for s in tuple(weights):
            shrink(s, limits.max_position_weight, ReasonCode.CONCENTRATION_CAP)
        # 5) 行业上限：某行业合计超标时，按各票占比等比缩到 max_sector_weight 内
        for sector in set(limits.sectors.get(s, "UNKNOWN") for s in weights):
            names = [s for s in weights if limits.sectors.get(s, "UNKNOWN") == sector]
            total = sum(weights[s] for s in names)
            if total > limits.max_sector_weight and total:
                for s in names:
                    shrink(s, weights[s] * limits.max_sector_weight / total, ReasonCode.SECTOR_CAP)
        # 6) 总仓上限：cap=min(总仓上限, 1-现金地板)；超了就全体等比缩
        cap = min(limits.max_exposure, 1 - limits.cash_floor)
        total = sum(weights.values())
        if total > cap and total:
            for s in tuple(weights):
                shrink(s, weights[s] * cap / total, ReasonCode.EXPOSURE_CAP)
        # 7) 组合波动上限：重算投影后的 √(w^TΣw)，超目标则按 factor 全体等比降杠杆
        names = tuple(sorted(weights))
        vector = np.array([weights[s] for s in names])
        matrix = (
            np.array([[float(covariance[a][b]) for b in names] for a in names])
            if names else np.empty((0, 0))
        )
        vol = sqrt(max(0.0, float(vector @ matrix @ vector))) if names else 0.0
        if vol > limits.max_portfolio_volatility and vol:
            factor = limits.max_portfolio_volatility / vol
            for s in tuple(weights):
                shrink(s, weights[s] * factor, ReasonCode.EXPOSURE_CAP)
        # 8) 换手上限：先扣卖出已用额度，余额只等比削“买入增量”
        buys = {
            s: max(0.0, w - current_weights.get(s, 0.0))
            for s, w in weights.items()
        }
        buy_turnover = sum(buys.values())
        allowed = max(
            0.0,
            limits.max_turnover - sum(
                max(0.0, current_weights.get(s, 0.0) - w)
                for s, w in weights.items()
            ),
        )
        if buy_turnover > allowed and buy_turnover:
            for s, amount in buys.items():
                shrink(
                    s,
                    current_weights.get(s, 0.0) + amount * allowed / buy_turnover,
                    ReasonCode.TURNOVER_CAP,
                )
        # 收尾：去零权、排序（可重放），汇总总仓/现金
        out = tuple(
            PortfolioWeight(symbol=s, weight=w)
            for s, w in sorted(weights.items()) if w > 0
        )
        exposure = sum(x.weight for x in out)
        return RiskProjectedPortfolio(
            trace_id=target.trace_id,
            target_portfolio_id=target.target_portfolio_id,
            snapshot_id=target.snapshot_id,
            weights=out,
            total_exposure=exposure,
            cash_weight=1 - exposure,
            adjustments=tuple(changes),
        )
