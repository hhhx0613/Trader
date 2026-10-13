"""从一个终结的 PositionEpisode 和 PIT 行情生成唯一客观 OutcomeCard。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from core.database.contracts import Fill, OutcomeCard, PositionEpisode
from core.database.reasons import ReasonCode


TerminalReason = Literal["horizon", "early_exit", "thesis_invalidated"]


@dataclass(frozen=True)
class OutcomeBuildResult:
    outcome: OutcomeCard | None
    reason_code: ReasonCode | None = None


class OutcomeCalculator:
    """纯计算节点；调用方负责调度、落账和 Episode revision。"""

    def build(
        self,
        *,
        trace_id: str,
        episode: PositionEpisode,
        fills: tuple[Fill, ...],
        terminal_reason: TerminalReason,
        as_of: datetime,
        prices: pd.DataFrame,
        market_regime: str | None = None,
        trigger_evidence_ids: tuple[str, ...] = (),
    ) -> OutcomeBuildResult:
        if as_of.tzinfo is None:
            raise ValueError("outcome as_of must be timezone-aware")
        if episode.status != "active":
            raise ValueError("only an active episode can be settled")
        frame = self._pit_frame(prices, as_of)
        if frame is None:
            return OutcomeBuildResult(None, ReasonCode.OUTCOME_DATA_MISSING)
        # 只统计归属本 episode 且有实际成交量的 Fill；零成交/越界 Fill 不参与结算
        related = tuple(fill for fill in fills if fill.fill_id in set(episode.fill_ids) and fill.filled_shares > 0)
        # 无成交本无法结算，但 carry-in 例外：它是既有持仓，本轮可能只 carry 不再买入
        if not related and not episode.is_carry_in:
            return OutcomeBuildResult(None, ReasonCode.OUTCOME_DATA_MISSING)
        basis = self._basis(episode, related)
        if basis is None:
            return OutcomeBuildResult(None, ReasonCode.OUTCOME_DATA_MISSING)
        anchor_at, entry_price, buy_notional, net_shares, commission = basis
        observed = self._observation(frame, episode, related, terminal_reason, as_of)
        if observed is None:
            return OutcomeBuildResult(None, ReasonCode.OUTCOME_NOT_DUE)
        observed_at, mark_price, path = observed
        # 卖出已实现 + 剩余仓位按 mark_price 计浮动 - 买入成本 = 毛收益；再扣佣金占比得净收益
        sell_proceeds = sum(fill.filled_shares * float(fill.executed_price)
                            for fill in related if fill.side == "sell" and fill.executed_price)
        gross_return = (sell_proceeds + net_shares * mark_price - buy_notional) / buy_notional
        net_return = gross_return - commission / buy_notional
        # 反事实基线：普通 thesis 假设“不交易”(0)；carry-in 假设“维持原持仓”(到期价/入场价-1)，
        # active_return 即真实动作相对基线的增量，用于区分“择股收益”与“随波逐流收益”
        counterfactual_return = 0.0
        counterfactual_kind = "no_trade"
        if episode.is_carry_in:
            counterfactual_kind = "maintain"
            counterfactual_return = (mark_price / float(episode.opening_price)) - 1.0
        # 持有期逐日相对入场价的收益曲线；最大回撤 = 相对滚动峰值(cummax)的最大跌幅
        returns = path / entry_price - 1.0
        max_drawdown = float((1.0 + returns).cummax().rdiv(1.0 + returns).sub(1.0).min())
        return OutcomeBuildResult(OutcomeCard(
            trace_id=trace_id, source_trace_id=episode.source_trace_id,
            episode_id=episode.episode_id, symbol=episode.symbol, observation=terminal_reason,
            observed_at=observed_at, fill_ids=tuple(fill.fill_id for fill in related),
            anchor_at=anchor_at, entry_price=entry_price, mark_price=mark_price,
            filled_shares=net_shares, gross_return=float(gross_return), net_return=float(net_return),
            is_carry_in=episode.is_carry_in, counterfactual_kind=counterfactual_kind,
            counterfactual_return=float(counterfactual_return), active_return=float(net_return - counterfactual_return),
            holding_trading_days=max(0, len(path) - 1),
            max_drawdown=max_drawdown, commission=commission,
            reason_codes=tuple(sorted({code for fill in related for code in fill.reason_codes}, key=str)),
            market_regime=market_regime, trigger_evidence_ids=trigger_evidence_ids,
        ))

    @staticmethod
    def _pit_frame(prices: pd.DataFrame, as_of: datetime) -> pd.DataFrame | None:
        required = {"open", "close", "available_at"}
        if prices.empty or not required.issubset(prices.columns):
            return None
        frame = prices.copy()
        # 双重 PIT 闸门：available_at(数据可得时刻) 和 index(行情所属日) 都不得晚于 as_of，
        # 防止把“晚到的数据”或“未来交易日”混入结算（回放时尤其关键）
        available = pd.to_datetime(frame["available_at"], utc=True)
        frame = frame.loc[(available <= pd.Timestamp(as_of)) &
                          (frame.index <= pd.Timestamp(as_of).tz_localize(None))]
        # 剔除非正价（停牌/坏数据）避免除零与伪回撤
        frame = frame[(frame["open"] > 0.0) & (frame["close"] > 0.0)].sort_index()
        return frame if not frame.empty else None

    @staticmethod
    def _basis(episode: PositionEpisode, fills: tuple[Fill, ...]) -> tuple[datetime, float, float, int, float] | None:
        buys = tuple(fill for fill in fills if fill.side == "buy" and fill.executed_price)
        if not buys and not episode.is_carry_in:
            return None
        # 累计买入股数/名义额：carry-in 把冻结的开盘持仓折进成本基，普通 thesis 只算本轮买入
        buy_shares = episode.opening_shares + sum(fill.filled_shares for fill in buys)
        buy_notional = ((episode.opening_shares * float(episode.opening_price)) if episode.is_carry_in else 0.0) + sum(fill.filled_shares * float(fill.executed_price) for fill in buys)
        sold = sum(fill.filled_shares for fill in fills if fill.side == "sell")
        net_shares = buy_shares - sold
        if buy_shares <= 0 or net_shares < 0:
            return None
        # 入场锚：carry-in 用 episode 锚点(无本轮买入)，否则用首笔买入成交时点
        anchor = episode.anchor_at if episode.is_carry_in else min(fill.executed_at for fill in buys)
        # entry_price = 加权平均成本(buy_notional/buy_shares)；commission 汇总全部 Fill 的手续费
        return (anchor, buy_notional / buy_shares, buy_notional,
                net_shares, sum(fill.commission for fill in fills))

    @staticmethod
    def _observation(frame: pd.DataFrame, episode: PositionEpisode, fills: tuple[Fill, ...],
                     terminal_reason: TerminalReason, as_of: datetime) -> tuple[datetime, float, pd.Series] | None:
        # path = 从入场锚点起的收盘价序列，用于回撤与持有期；mark_price 取哪个时点由终结原因决定
        anchor = pd.Timestamp(episode.anchor_at).tz_localize(None).normalize()
        path = frame.loc[frame.index >= anchor, "close"]
        if path.empty:
            return None
        if terminal_reason == "horizon":
            # 到期结算：取到期日当天(或之后第一个交易日)的收盘为 mark，path 截断到该日；未到期则不结算
            due = pd.Timestamp(episode.current_due_at).tz_localize(None).normalize()
            due_rows = frame.loc[frame.index >= due]
            if due_rows.empty:
                return None
            at = due_rows.index[0]
            return at.to_pydatetime().replace(tzinfo=as_of.tzinfo), float(due_rows.iloc[0]["close"]), path.loc[path.index <= at]
        exits = [fill for fill in fills if fill.side == "sell" and fill.executed_price]
        if terminal_reason == "early_exit" and exits:
            # 提前清仓：以最后一笔卖出为结算点，mark 用实际卖价(而非收盘)，path 截到卖出日
            latest = max(exits, key=lambda fill: fill.executed_at)
            path = path.loc[path.index <= pd.Timestamp(latest.executed_at).tz_localize(None).normalize()]
            if path.empty:
                return None
            return latest.executed_at, float(latest.executed_price), path
        # 其余(如 thesis_invalidated)：按当前可得的最后一根收盘估 mark，持有到 as_of
        row = frame.iloc[-1]
        return frame.index[-1].to_pydatetime().replace(tzinfo=as_of.tzinfo), float(row["close"]), path
