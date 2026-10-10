"""将研究动作编译为确定性的仓位边界。"""

from collections.abc import Mapping

from core.research.contracts import IntentConstraints, PortfolioIntent, WeightConstraint
from core.research.reasons import ReasonCode


class IntentConstraintBuilder:
    """把 Committee 的离散动作确定性编译成优化器可用的权重边界；不预测收益、不定最终权重。"""
    def __init__(
        self,
        *,
        max_position_weight: float,
        hold_band: float,
        seed_weight: float,
        max_holdings: int,
    ) -> None:
        if not 0.0 < max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in (0, 1]")
        if (
            not 0.0 <= hold_band <= 1.0
            or not 0.0 < seed_weight <= max_position_weight
            or max_holdings < 1
        ):
            raise ValueError("invalid constraint configuration")
        self.max_position_weight = max_position_weight
        self.hold_band = hold_band
        self.seed_weight = seed_weight
        self.max_holdings = max_holdings

    def build(
        self,
        intent: PortfolioIntent,
        current_weights: Mapping[str, float],
        *,
        trace_id: str,
    ) -> IntentConstraints:
        # 进入逐票翻译前，先按优先级把 MAX_HOLDINGS 名额分完：
        #   retained = 当前已持有且不被退出的票（天然占坑）；
        #   admitted = 新开长仓里按 priority 录取的票，只补满 (max_holdings - retained) 个名额。
        # priority 只是"够不够格入场"的开关，绝不参与权重大小或收益评分。
        retained = sum(
            1 for item in intent.items
            if current_weights.get(item.symbol, 0.0) > 0 and item.action != "exit"
        )
        new_longs = sorted(
            (x for x in intent.items
             if x.action == "long" and current_weights.get(x.symbol, 0.0) == 0),
            key=lambda x: x.priority,
        )
        admitted = {
            item.symbol for item in new_longs[:max(0, self.max_holdings - retained)]
        }
        items: list[WeightConstraint] = []
        for item in intent.items:
            current = float(current_weights.get(item.symbol, 0.0))
            if not 0.0 <= current <= 1.0:
                raise ValueError(f"invalid current weight for {item.symbol}")
            if item.action == "exit":
                # 义务性清仓：钉到 0，force_exit 供下游识别为强制卖
                bound = WeightConstraint(
                    symbol=item.symbol,
                    min_weight=0.0, max_weight=0.0,
                    trading_allowed=True, force_exit=True,
                )
            elif item.action == "abstain":
                bound = WeightConstraint(
                    symbol=item.symbol,
                    min_weight=current, max_weight=current,
                    trading_allowed=False,
                    reason_codes=(ReasonCode.INSUFFICIENT_EVIDENCE,),
                )
            elif item.action == "hold":
                bound = WeightConstraint(
                    symbol=item.symbol,
                    min_weight=max(0.0, current - self.hold_band),
                    max_weight=min(self.max_position_weight, current + self.hold_band),
                    trading_allowed=True,
                )
            elif item.action == "reduce":
                # strength 只决定减仓上限，绝不成为收益预测或买入权重乘数
                cap = min(self.max_position_weight, current * (1.0 - 0.25 * item.strength))
                bound = WeightConstraint(
                    symbol=item.symbol,
                    min_weight=0.0, max_weight=cap,
                    trading_allowed=True,
                )
            else:  # long
                # 三态落点：①空仓且抢到名额→下限 seed_weight；
                # ②已持有→[0, max_position_weight] 自由调；
                # ③没抢到名额→[0,0] 不可交易。
                allowed = current > 0.0 or item.symbol in admitted
                bound = WeightConstraint(
                    symbol=item.symbol,
                    min_weight=(self.seed_weight if current == 0.0 and allowed else 0.0),
                    max_weight=self.max_position_weight if allowed else 0.0,
                    trading_allowed=allowed,
                )
            items.append(bound)
        return IntentConstraints(
            trace_id=trace_id,
            intent_id=intent.intent_id,
            snapshot_id=intent.snapshot_id,
            items=tuple(items),
        )
