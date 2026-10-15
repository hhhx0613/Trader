"""统一研究编排 / One stage-gated orchestration from collection to PortfolioIntent.

阶段门控集中在此：CLI、外部定时调度与观测工具共用同一函数，避免同串编排散落在
多个脚本里各写一遍（Plan.md 3.1.1「编排入口收敛」）。

三种节奏（``FlowOptions.mode``）：
  daily    采集 -> 建卡入池，不冻结。不碰模型，定时任务因此不需要 API key
  collect  采集 -> 建卡 -> 用本轮卡集点名冻结，一次得到一份可复现 Snapshot
  decide   按 ``available_from`` 从卡片池池选卡冻结 -> 逐标的研究图 -> Committee。
           全程不联网：读的都是每日 daily 已经入池的卡

三条边界：
  - 要决策就必须给真实账户。``decide`` 缺 ``model`` 或缺 ``account_state`` 直接
    ``ValueError``，不退回占位账户：Risk Critic 的 prompt 写明「缺 limits 或持仓
    状态不得当作放行」，占位账户只会烧出一轮全 ``abstain`` 的无效意图。
  - 观测不属于编排。逐节点打印是调用方对模型客户端的包装，本模块不含 print、
    事件总线或前端耦合。
  - 采集能力只在本模块这一侧。研究节点经 ``DataGateway`` 读冻结卡，拿不到任何
    采集入口，缺证据走显式 ``data_request`` 弃权，由下一轮 daily 补齐。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd

from core import config
from core.collect import filings as filings_collector
from core.collect import news as news_collector
from core.collect.market import INDICATORS_VERSION, build_feature_card, load_price_frame, prepare_market_history
from core.decision import ExecutionAccount, ExecutionLimits, SimulatedExecution
from core.database import DataGateway, ResearchLedger
from core.database.contracts import EvidenceCard, Fill, PortfolioIntent, PositionEpisode, ResearchPacket, ResearchSnapshot
from core.database.raw import RawPayloadStore
from core.database.snapshot import SnapshotBuilder
from core.decision.runtime import build_policy_and_inputs, load_risk_state
from core.decision.planner import PortfolioPolicyResult

from agents.research.committee import Committee
from agents.research.graph import PerAssetResearchGraph
from agents.research.types import JsonModel

MODES = ("daily", "collect", "decide", "execute")

# decide 所需账户字段：与 Risk Critic 的四项注入上下文同源，regime 可缺（只影响措辞、
# 不影响放行与否），但缺现金/持仓/限额时必须拒绝，否则 Critic 只能在空洞上签字。
_REQUIRED_ACCOUNT_KEYS = ("cash", "positions", "limits")


@dataclass(frozen=True)
class FlowOptions:
    """一轮编排的口径参数；除 ``as_of`` 外都不给隐式默认。"""

    pool: tuple[str, ...]
    mode: str = "daily"
    as_of: datetime | None = None          # None = 实盘：采集完成后才定时点
    news_days: int = 7
    market_lookback: int = 220
    filings: bool = True
    account_state: dict[str, Any] | None = None
    order_plan_id: str | None = None
    trace_prefix: str = "flow"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown flow mode: {self.mode}")
        normalized = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in self.pool))
        if not normalized or any(not symbol for symbol in normalized):
            raise ValueError("flow requires at least one non-empty symbol")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("flow as_of must be timezone-aware")
        object.__setattr__(self, "pool", normalized)


@dataclass(frozen=True)
class RoundResult:
    """本轮产出；daily 只有建卡数，decide 一路带到 PortfolioIntent。"""

    mode: str
    as_of: datetime
    ingested: int
    snapshot: ResearchSnapshot | None = None
    packets: tuple[ResearchPacket, ...] = ()
    intent: PortfolioIntent | None = None
    portfolio: PortfolioPolicyResult | None = None
    evidence: int = 0
    risk_state: dict[str, Any] | None = None
    risk_triggered: bool = False
    fills: tuple[Fill, ...] = ()
    episodes: tuple[PositionEpisode, ...] = ()


def run_research_round(options: FlowOptions, *, model: JsonModel | None = None,
                       ledger: ResearchLedger | None = None,
                       raw_store: RawPayloadStore | None = None) -> RoundResult:
    """按 ``mode`` 执行到对应阶段为止；只做编排，不新增任何研究或采集逻辑。

    网络、落库、PIT 校验、去重全部委派给 ``core/collect/*`` 与 ``core/database``。
    ``ledger``/``raw_store`` 供调用方（含离线测试与常驻调度器）复用已打开的连接，
    省略时按 ``config`` 的主线存储路径新建，并在返回前关掉自己打开的那个正文库。
    """
    if options.mode == "decide":
        _require_decision_inputs(model, options.account_state)
    if options.mode == "execute" and (options.account_state is None or not options.order_plan_id):
        raise ValueError("execute requires account_state and an explicit order_plan_id")

    book = ledger or ResearchLedger(config.RESEARCH_LEDGER_PATH)
    store = raw_store or RawPayloadStore(config.RESEARCH_RAW_DIR)
    try:
        return _round(options, model=model, book=book, store=store)
    finally:
        if raw_store is None:
            store.close()  # 自开的库自关；调用方交来的连接归调用方


def _round(options: FlowOptions, *, model: JsonModel | None, book: ResearchLedger,
           store: RawPayloadStore) -> RoundResult:
    """阶段门控本体；前置校验与连接生命周期已由 ``run_research_round`` 接手。"""
    if options.mode == "execute":
        return _execute_order_plan(options, book, store)
    builder = SnapshotBuilder(book, store)

    as_of = options.as_of
    cards: tuple[EvidenceCard, ...] = ()
    with _trace_guard(options.trace_prefix):
        if options.mode in ("daily", "collect"):
            # 先按当前时点算采集窗口，再在采集结束后重新定时点：实盘模式本轮拉到的
            # 数据本轮即可用；传了 --as-of 回放时时点钉死，晚到的数据被 PIT 闸门拒绝。
            window = as_of or datetime.now(timezone.utc)
            records = _collect(store, options, window)
            as_of = datetime.now(timezone.utc) if options.as_of is None else options.as_of
            for symbol in options.pool:
                feature = build_feature_card(symbol, as_of, lookback_days=options.market_lookback, store=store)
                if feature is not None:
                    records.append(feature)

        as_of = as_of or datetime.now(timezone.utc)
        stamp = as_of.strftime("%Y%m%dT%H%M%SZ")
        if options.mode in ("daily", "collect"):
            cards = tuple(builder.ingest_records(trace_id=f"{options.trace_prefix}-ingest-{stamp}",
                                                 as_of=as_of, records=records))

        if options.mode == "daily":
            if options.account_state is None:
                return RoundResult(mode="daily", as_of=as_of, ingested=len(cards))
            previous = dict(options.account_state.get("risk_state") or {})
            risk_state = load_risk_state(options.account_state, options.pool, as_of, store)
            # 纯函数只返回状态；入口负责把它交还给真实账户/回测账户以持久化 peak_equity。
            options.account_state["risk_state"] = risk_state
            risk_triggered = bool(risk_state["stop_loss"]) or (
                bool(risk_state["risk_locked"]) and not bool(previous.get("risk_locked", False)))
            if not risk_triggered:
                return RoundResult(mode="daily", as_of=as_of, ingested=len(cards), risk_state=risk_state)
            # 正常 daily 不碰模型；硬风险是唯一允许升级到既有 decide 全流程的条件。
            _require_decision_inputs(model, options.account_state)
            decision = _round(
                FlowOptions(pool=options.pool, mode="decide", as_of=as_of, news_days=options.news_days,
                            market_lookback=options.market_lookback, filings=options.filings,
                            account_state=options.account_state, trace_prefix=options.trace_prefix),
                model=model, book=book, store=store,
            )
            return replace(decision, mode="daily", ingested=len(cards), risk_state=risk_state,
                           risk_triggered=True)

        source_versions = {"news": news_collector.NEWS_VERSION, "market": INDICATORS_VERSION,
                           "filings": filings_collector.FILINGS_VERSION}
        freeze_trace = f"{options.trace_prefix}-freeze-{stamp}"
        if options.mode == "collect":
            # collect 只验证数据链、不跑模型，缺账户时用明写的占位口径冻结（与 daily 同
            # 为「不产生决策」的节奏）；要拿到可执行意图请走 decide + 真实 account_state。
            snapshot = builder.freeze_snapshot(trace_id=freeze_trace, as_of=as_of, symbols=options.pool,
                                               source_versions=source_versions,
                                               account_state=options.account_state or {"as_of": as_of.isoformat(),
                                                                                       "note": "collect 不跑模型：占位账户"},
                                               evidence_ids=tuple(card.evidence_id for card in cards))
        else:
            # 决策日只冻结，不建卡：卡池由每日 daily 积累，本轮按可得窗口选卡。
            snapshot = builder.freeze_snapshot(trace_id=freeze_trace, as_of=as_of, symbols=options.pool,
                                               source_versions=source_versions,
                                               account_state=dict(options.account_state),
                                               available_from=as_of - timedelta(days=options.news_days))

        if model is None:
            return RoundResult(mode=options.mode, as_of=as_of, ingested=len(cards), snapshot=snapshot,
                               evidence=len(snapshot.evidence_ids))

        gateway = DataGateway(book, store)
        graph = PerAssetResearchGraph(ledger=book, gateway=gateway, model=model)
        packets = tuple(
            graph.run(snapshot_id=snapshot.snapshot_id, symbol=symbol, as_of=snapshot.as_of,
                      trace_id=f"{options.trace_prefix}-research-{stamp}-{symbol}")
            for symbol in options.pool)
        intent = Committee(ledger=book, gateway=gateway, model=model).run(
            snapshot_id=snapshot.snapshot_id, packet_ids=tuple(packet.packet_id for packet in packets),
            as_of=snapshot.as_of, trace_id=f"{options.trace_prefix}-committee-{stamp}")
        policy, current, volatilities, covariance, trading, risk_state = build_policy_and_inputs(
            dict(options.account_state), options.pool, snapshot.as_of, store)
        portfolio_trace = f"{options.trace_prefix}-portfolio-{stamp}"
        book.start_trace(portfolio_trace, snapshot.as_of)
        portfolio = policy.build(intent, trace_id=portfolio_trace, current_weights=current,
                                 volatilities=volatilities, covariance=covariance, trading=trading,
                                 drawdown=float(risk_state["drawdown"]),
                                 untradable=set(trading.untradable_symbols), stop_loss=set(risk_state["stop_loss"]),
                                 risk_locked=bool(risk_state["risk_locked"]), created_at=as_of)
        book.append_constraints(portfolio.constraints)
        book.append_target_portfolio(portfolio.target)
        book.append_risk_projected_portfolio(portfolio.projected)
        book.append_order_plan(portfolio.order_plan)
        _record_carry_in_episodes(book, intent, portfolio.order_plan, options.account_state, snapshot.as_of, portfolio_trace)
        book.complete_trace(portfolio_trace, snapshot.as_of)
    return RoundResult(mode=options.mode, as_of=as_of, ingested=len(cards), snapshot=snapshot,
                       packets=packets, intent=intent, portfolio=portfolio, evidence=len(snapshot.evidence_ids))


def _execute_order_plan(options: FlowOptions, book: ResearchLedger, store: RawPayloadStore) -> RoundResult:
    """人工 T 日入口：只执行已冻结订单，绝不在 decide 当日假造下一日成交。"""
    assert options.order_plan_id is not None and options.account_state is not None and options.as_of is not None
    plan = book.get_order_plan(options.order_plan_id)
    frames = _execution_frames(plan, options.as_of, store)
    # 只在所有标的都有行情的交集日里选，避免半仓可成交
    days = set.intersection(*(set(frame.index) for frame in frames.values())) if frames else set()
    # T+1 边界：可成交日必须严格晚于决策日，绝不在 decide 当日假造成交
    eligible = sorted(day for day in days if day > plan.created_at.replace(tzinfo=None))
    if not eligible:
        raise ValueError("no PIT-tradable T-day after OrderPlan decision")
    execution_day = eligible[0]
    # 行情索引是无时区的日，成交时点回填 as_of 的 tzinfo 以保持账本时间戳一致
    executed_at = execution_day.to_pydatetime().replace(tzinfo=options.as_of.tzinfo)
    holdings = {str(item["symbol"]).upper(): int(item["quantity"])
                for item in options.account_state["positions"]}
    result = SimulatedExecution().execute(
        plan, trace_id=f"{options.trace_prefix}-execution-{options.as_of.strftime('%Y%m%d%H%M%S')}",
        decision_at=plan.created_at, executed_at=executed_at,
        account=ExecutionAccount(cash=float(options.account_state["cash"]), holdings=holdings),
        open_prices={symbol: float(frame.loc[execution_day, "open"]) for symbol, frame in frames.items()},
        volumes={symbol: float(frame.loc[execution_day, "volume"]) for symbol, frame in frames.items()},
        limits=ExecutionLimits(max_participation_rate=float(config.COST_LIMITS["max_participation_rate"]),
                               slippage=float(config.SLIPPAGE),
                               commission_per_order=float(config.COST_MODEL["commission_per_order"])),
    )
    trace_id = result.fills[0].trace_id if result.fills else f"{options.trace_prefix}-execution-empty"
    book.start_trace(trace_id, executed_at)
    for fill in result.fills:
        book.append_fill(fill)
    episodes = _record_execution_episodes(book, plan, result.fills, executed_at, trace_id)
    book.complete_trace(trace_id, executed_at)
    return RoundResult(mode="execute", as_of=options.as_of, ingested=0,
                       fills=result.fills, episodes=tuple(episodes))


def _record_execution_episodes(book: ResearchLedger, plan, fills: tuple[Fill, ...],
                               executed_at: datetime, trace_id: str) -> list[PositionEpisode]:
    """把实际 Fill 接到唯一活动 Episode；零成交只保留 Fill，不产生案例。"""
    projected = book.get_risk_projected_portfolio(plan.projected_portfolio_id)
    target = book.get_target_portfolio(projected.target_portfolio_id)
    intent = book.get_intent(target.intent_id)
    by_symbol = {item.symbol: item for item in intent.items}
    revisions: list[PositionEpisode] = []
    for fill in fills:
        if fill.filled_shares <= 0:
            # 零成交不建案例：否则 OutcomeReviewFlow 会对着空仓位做无意义复盘
            continue
        item = by_symbol.get(fill.symbol)
        if item is None:
            continue
        active = book.find_active_position_episode(fill.symbol)
        if active is None:
            # 无活动 episode 时，只有买入且显式 start 才是新论文起点，其余一律不凭空建仓
            if fill.side != "buy" or item.horizon_action != "start" or item.horizon_days not in {5, 20, 60}:
                continue
            due_at = (pd.Timestamp(executed_at) + pd.offsets.BDay(item.horizon_days)).to_pydatetime()
            episode = PositionEpisode(
                trace_id=trace_id, symbol=fill.symbol, source_trace_id=plan.trace_id, status="active",
                anchor_at=executed_at, current_due_at=due_at, original_horizon_days=item.horizon_days,
                intent_item_ids=(item.intent_item_id,), order_plan_ids=(plan.order_plan_id,), fill_ids=(fill.fill_id,),
            )
        else:
            due_at = active.current_due_at
            # retain 不动计时器；shorten 只能把到期日提前，防止调仓无限延后复盘。
            if item.horizon_action == "shorten" and item.horizon_days in {5, 20, 60}:
                requested_due = (pd.Timestamp(executed_at) + pd.offsets.BDay(item.horizon_days)).to_pydatetime()
                due_at = min(due_at, requested_due)
            episode = active.model_copy(update={
                "trace_id": trace_id,
                "created_at": executed_at,
                "revision": active.revision + 1,
                "current_due_at": due_at,
                # dict.fromkeys 保序去重：同一 episode 被多次修订时 fill/intent/order id 不重复膨胀
                "intent_item_ids": tuple(dict.fromkeys((*active.intent_item_ids, item.intent_item_id))),
                "order_plan_ids": tuple(dict.fromkeys((*active.order_plan_ids, plan.order_plan_id))),
                "fill_ids": tuple(dict.fromkeys((*active.fill_ids, fill.fill_id))),
            })
        book.append_position_episode(episode)
        revisions.append(episode)
    return revisions


def _record_carry_in_episodes(book: ResearchLedger, intent: PortfolioIntent, plan, account_state: dict[str, Any],
                              as_of: datetime, trace_id: str) -> list[PositionEpisode]:
    """已终结但仍持有的仓位，只有在下一轮明确 start 新 thesis 时才开启后继 episode。"""
    positions = {str(row["symbol"]).upper(): row for row in account_state.get("positions", ())
                 if int(row.get("quantity", 0)) > 0}
    created: list[PositionEpisode] = []
    for item in intent.items:
        position = positions.get(item.symbol)
        if position is None or book.find_active_position_episode(item.symbol) is not None:
            # 已持有但已有活动 episode 的不再 carry-in，防同一标的双开计时器
            continue
        if item.action != "long" or item.horizon_action != "start" or item.horizon_days not in {5, 20, 60}:
            continue
        price = position.get("last_price")
        if price is None or float(price) <= 0.0:
            # carry-in 开盘成本必须取冻结 last_price，缺价直接拒绝而非估算，避免污染后续收益口径
            raise ValueError(f"carry-in requires a frozen positive last_price for {item.symbol}")
        due_at = (pd.Timestamp(as_of) + pd.offsets.BDay(item.horizon_days)).to_pydatetime()
        episode = PositionEpisode(
            trace_id=trace_id, symbol=item.symbol, source_trace_id=plan.trace_id, status="active",
            anchor_at=as_of, current_due_at=due_at, original_horizon_days=item.horizon_days,
            is_carry_in=True, opening_shares=int(position["quantity"]), opening_price=float(price),
            intent_item_ids=(item.intent_item_id,), order_plan_ids=(plan.order_plan_id,),
        )
        book.append_position_episode(episode)
        created.append(episode)
    return created


def _execution_frames(plan, as_of: datetime, store: RawPayloadStore):
    start = plan.created_at.strftime("%Y-%m-%d")
    end = as_of.strftime("%Y-%m-%d")
    frames = {}
    for order in plan.orders:
        frame = load_price_frame(order.symbol, start, end, store=store)
        # available_at<=as_of 的 PIT 闸门：回放时挡住晚于执行时点的未来行情
        frame = frame[frame["available_at"] <= as_of] if not frame.empty else frame
        if frame.empty:
            raise ValueError(f"missing PIT execution prices for {order.symbol}")
        frames[order.symbol] = frame
    return frames


def _collect(store: RawPayloadStore, options: FlowOptions, window: datetime) -> list[dict[str, Any]]:
    """采集新闻与披露记录 + 保证行情素材批次已入库；K 线批次是素材不是事实，不进 Snapshot。"""
    end = window.strftime("%Y-%m-%d")
    news_start = (window - timedelta(days=options.news_days)).strftime("%Y-%m-%d")
    records: list[dict[str, Any]] = []
    for symbol in options.pool:
        records.extend(news_collector.fetch_news_records(symbol, news_start, end, store=store,
                                                          include_fresh=options.as_of is None))
        if options.filings:
            records.extend(filings_collector.fetch_filings_records(symbol, news_start, end))
    # 持仓不一定仍在研究池中；daily 仍须先补齐其行情，才能对它做止损和回撤检查。
    held = tuple(str(position["symbol"]).upper() for position in (options.account_state or {}).get("positions", ()))
    for symbol in tuple(dict.fromkeys((*options.pool, *held))):
        prepare_market_history(symbol, window, store=store, include_fresh=options.as_of is None)
    return records


def _require_decision_inputs(model: JsonModel | None, account_state: dict[str, Any] | None) -> None:
    """决策节奏的前置把关：没有模型跑不出意图，没有真实账户的意图不该花钱。"""
    if model is None:
        raise ValueError("decide requires an injected model; use mode=daily or mode=collect for data-only rounds")
    missing = [key for key in _REQUIRED_ACCOUNT_KEYS if key not in (account_state or {})]
    if missing:
        raise ValueError(f"decide requires account_state with real position data; missing {missing}")


@contextmanager
def _trace_guard(prefix: str) -> Iterator[None]:
    """把 trace 主键冲突翻成调度器能读懂的一句话。

    trace_id 取自 ``as_of`` 的秒级戳，同一时点重跑会撞主键。撞了是「这一轮已经记过账」，
    不是崩溃：让调度器按幂等信号处理，比在日志里留一段 sqlite 栈更容易归因。
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            f"ledger already holds a trace for this as_of (prefix {prefix}); "
            f"rerun with a different --as-of instead of overwriting history"
        ) from exc
